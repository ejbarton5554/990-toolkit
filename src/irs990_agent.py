"""
IRS 990 XML Agentic Code Generator & Tester
=============================================
An agentic loop that:
  1. Accepts a user query about IRS 990 data
  2. Generates Python code to extract/interpret that data from 990 XML files
  3. Executes the code in a sandbox
  4. Validates the output
  5. Self-corrects on errors (up to N retries)

Requirements:
  pip install anthropic lxml

Usage:
  # Interactive mode
  python irs990_agent.py

  # Single query mode
  python irs990_agent.py --query "Find total revenue" --xml-dir ./990_xmls

  # With a specific XML file
  python irs990_agent.py --query "Find CEO compensation" --xml-file ./990.xml
"""

import os
import sys
import json
import subprocess
import tempfile
import textwrap
import traceback
import argparse
from pathlib import Path
from datetime import datetime

try:
    import anthropic
except ImportError:
    sys.exit("Missing dependency: pip install anthropic")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
MAX_RETRIES = 5          # Max self-correction iterations per query
MODEL = "claude-sonnet-4-20250514"
TIMEOUT_SECONDS = 30     # Max runtime for generated code
SANDBOX_DIR = tempfile.mkdtemp(prefix="irs990_agent_")

# Common IRS 990 XML namespaces (the agent will also discover these dynamically)
IRS_990_NAMESPACES = {
    "efile": "http://www.irs.gov/efile",
    "xsi": "http://www.w3.org/2001/XMLSchema-instance",
}

# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = textwrap.dedent("""\
You are an expert Python developer specializing in parsing IRS 990 tax form
XML data. You write clean, robust, well-commented Python code.

KEY FACTS ABOUT IRS 990 XML FILES:
- They use the namespace "http://www.irs.gov/efile" (commonly).
- The root element is usually <Return> with a <ReturnHeader> and <ReturnData>.
- Inside <ReturnData> you'll find form-specific elements like <IRS990>,
  <IRS990EZ>, <IRS990PF>, <IRS990ScheduleA>, etc.
- Field names are PascalCase (e.g., <TotalRevenueAmt>, <OfficerCompensationAmt>).
- Older filings may use different element names (e.g., <TotalRevenue> vs
  <TotalRevenueAmt>) — always handle both when possible.
- Namespaces can vary across filing years. Always discover the namespace
  dynamically from the root element if possible.

RULES FOR CODE YOU GENERATE:
1. Use `lxml.etree` for XML parsing (it handles namespaces well).
2. Always discover the default namespace dynamically from the XML root.
3. Wrap logic in a `main(xml_path)` function that accepts a file path.
4. Print results as structured JSON to stdout so the harness can capture it.
5. Handle missing elements gracefully — return None/null, don't crash.
6. Include brief comments explaining what each section does.
7. The code must be completely self-contained (no imports beyond stdlib + lxml).
8. If multiple XML files are provided via a directory, process each one.
""")

CODE_GEN_PROMPT = textwrap.dedent("""\
USER QUERY: {query}

XML FILE(S): {xml_paths}

Here is a sample of the XML structure (first 200 lines of the first file):
```xml
{xml_sample}
```

Write a COMPLETE, SELF-CONTAINED Python script that:
1. Parses the IRS 990 XML file(s) listed above.
2. Extracts the data the user is asking about.
3. Prints the results as a JSON object to stdout.

Return ONLY the Python code inside a ```python ... ``` block. No other text.
""")

FIX_PROMPT = textwrap.dedent("""\
The code you generated failed. Here is the context:

ORIGINAL QUERY: {query}
XML FILE(S): {xml_paths}

YOUR PREVIOUS CODE:
```python
{code}
```

ERROR OUTPUT:
```
{error}
```

STDOUT (if any):
```
{stdout}
```

Please fix the code. Return ONLY the corrected Python code inside a
```python ... ``` block. No other text.
""")

VALIDATE_PROMPT = textwrap.dedent("""\
I asked an agent to extract IRS 990 data for this query:
  "{query}"

The code produced this JSON output:
```json
{output}
```

Evaluate the output:
1. Does it look like valid, plausible IRS 990 data for the query?
2. Are there any obvious issues (all nulls, wrong data types, nonsensical values)?

Respond with a JSON object:
{{
  "valid": true/false,
  "issues": "description of any issues or empty string",
  "suggestions": "suggestions for improvement or empty string"
}}

Return ONLY the JSON. No other text.
""")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
class AgentResult:
    """Holds the result of one agentic loop iteration."""

    def __init__(self):
        self.query: str = ""
        self.code: str = ""
        self.stdout: str = ""
        self.stderr: str = ""
        self.success: bool = False
        self.output_data: dict | list | None = None
        self.validation: dict | None = None
        self.iterations: int = 0
        self.history: list[dict] = []  # log of each iteration

    def summary(self) -> str:
        status = "✅ SUCCESS" if self.success else "❌ FAILED"
        lines = [
            f"\n{'='*60}",
            f"  {status} after {self.iterations} iteration(s)",
            f"  Query: {self.query}",
            f"{'='*60}",
        ]
        if self.success and self.output_data is not None:
            lines.append(json.dumps(self.output_data, indent=2, default=str))
        elif not self.success:
            lines.append(f"Last error:\n{self.stderr[-500:]}")
        if self.validation:
            lines.append(f"\nValidation: {json.dumps(self.validation, indent=2)}")
        return "\n".join(lines)


def get_xml_sample(xml_path: str, max_lines: int = 200) -> str:
    """Read the first N lines of an XML file for context."""
    try:
        with open(xml_path, "r", encoding="utf-8", errors="replace") as f:
            lines = []
            for i, line in enumerate(f):
                if i >= max_lines:
                    break
                lines.append(line.rstrip())
            return "\n".join(lines)
    except Exception as e:
        return f"[Error reading XML: {e}]"


def resolve_xml_paths(xml_file: str | None, xml_dir: str | None) -> list[str]:
    """Resolve XML file paths from arguments."""
    paths = []
    if xml_file:
        p = Path(xml_file)
        if p.exists() and p.suffix.lower() == ".xml":
            paths.append(str(p.resolve()))
    if xml_dir:
        d = Path(xml_dir)
        if d.is_dir():
            for f in sorted(d.glob("*.xml")):
                paths.append(str(f.resolve()))
    return paths


def extract_code_block(text: str) -> str:
    """Extract Python code from a markdown code block."""
    # Try ```python ... ```
    if "```python" in text:
        start = text.index("```python") + len("```python")
        end = text.index("```", start)
        return text[start:end].strip()
    # Try ``` ... ```
    if "```" in text:
        start = text.index("```") + 3
        end = text.index("```", start)
        return text[start:end].strip()
    # Assume the entire response is code
    return text.strip()


def extract_json(text: str) -> dict:
    """Best-effort JSON extraction from LLM response."""
    text = text.strip()
    # Strip markdown fences
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if "```" in text:
            text = text[:text.rindex("```")]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"valid": False, "issues": "Could not parse validation response", "suggestions": ""}


# ---------------------------------------------------------------------------
# Core agent functions
# ---------------------------------------------------------------------------
def call_llm(client: anthropic.Anthropic, system: str, user_message: str) -> str:
    """Call the Anthropic API and return the assistant's text."""
    response = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text


def generate_code(client: anthropic.Anthropic, query: str, xml_paths: list[str], xml_sample: str) -> str:
    """Ask the LLM to generate extraction code."""
    prompt = CODE_GEN_PROMPT.format(
        query=query,
        xml_paths=json.dumps(xml_paths),
        xml_sample=xml_sample[:8000],  # limit context size
    )
    raw = call_llm(client, SYSTEM_PROMPT, prompt)
    return extract_code_block(raw)


def fix_code(client: anthropic.Anthropic, query: str, xml_paths: list[str],
             code: str, error: str, stdout: str) -> str:
    """Ask the LLM to fix broken code."""
    prompt = FIX_PROMPT.format(
        query=query,
        xml_paths=json.dumps(xml_paths),
        code=code,
        error=error[-3000:],
        stdout=stdout[-1000:],
    )
    raw = call_llm(client, SYSTEM_PROMPT, prompt)
    return extract_code_block(raw)


def validate_output(client: anthropic.Anthropic, query: str, output: str) -> dict:
    """Ask the LLM to validate the extracted data."""
    prompt = VALIDATE_PROMPT.format(query=query, output=output[:4000])
    raw = call_llm(client, "You are a data validation expert.", prompt)
    return extract_json(raw)


def execute_code(code: str) -> tuple[str, str, int]:
    """
    Write code to a temp file and execute it in a subprocess.
    Returns (stdout, stderr, returncode).
    """
    script_path = os.path.join(SANDBOX_DIR, "generated_script.py")
    with open(script_path, "w") as f:
        f.write(code)

    try:
        result = subprocess.run(
            [sys.executable, script_path],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
            cwd=SANDBOX_DIR,
        )
        return result.stdout, result.stderr, result.returncode
    except subprocess.TimeoutExpired:
        return "", f"TIMEOUT: Code exceeded {TIMEOUT_SECONDS}s limit", 1
    except Exception as e:
        return "", f"EXECUTION ERROR: {e}", 1


# ---------------------------------------------------------------------------
# Main agentic loop
# ---------------------------------------------------------------------------
def run_agent(
    client: anthropic.Anthropic,
    query: str,
    xml_paths: list[str],
    max_retries: int = MAX_RETRIES,
    validate: bool = True,
) -> AgentResult:
    """
    The core agentic loop:
      1. Generate code from the query + XML sample
      2. Execute the code
      3. If error → ask LLM to fix → re-execute (up to max_retries)
      4. If success → optionally validate output with LLM
      5. If validation fails → feed suggestions back and regenerate
    """
    result = AgentResult()
    result.query = query

    if not xml_paths:
        result.stderr = "No XML files found. Provide --xml-file or --xml-dir."
        return result

    xml_sample = get_xml_sample(xml_paths[0])
    print(f"\n🤖 Agent starting | Query: {query}")
    print(f"   XML files: {len(xml_paths)}")

    # --- Step 1: Initial code generation ---
    print(f"\n📝 [Iteration 1] Generating code...")
    code = generate_code(client, query, xml_paths, xml_sample)
    result.code = code

    for iteration in range(1, max_retries + 1):
        result.iterations = iteration
        iter_log = {"iteration": iteration, "action": "", "success": False}

        # --- Step 2: Execute ---
        print(f"▶️  [Iteration {iteration}] Executing code...")
        stdout, stderr, returncode = execute_code(code)
        result.stdout = stdout
        result.stderr = stderr

        iter_log["returncode"] = returncode
        iter_log["stdout_preview"] = stdout[:200]
        iter_log["stderr_preview"] = stderr[:200]

        if returncode != 0:
            # --- Step 3: Fix on error ---
            print(f"❌ [Iteration {iteration}] Execution failed:\n   {stderr[:150]}")
            iter_log["action"] = "fix"
            result.history.append(iter_log)

            if iteration < max_retries:
                print(f"🔧 [Iteration {iteration}] Asking LLM to fix...")
                code = fix_code(client, query, xml_paths, code, stderr, stdout)
                result.code = code
                continue
            else:
                print(f"💀 Max retries reached. Giving up.")
                break

        # Execution succeeded — try to parse JSON from stdout
        try:
            result.output_data = json.loads(stdout)
        except json.JSONDecodeError:
            # Output isn't JSON; store raw text
            result.output_data = {"raw_output": stdout.strip()}

        # --- Step 4: Validate ---
        if validate:
            print(f"🔍 [Iteration {iteration}] Validating output...")
            validation = validate_output(client, query, stdout)
            result.validation = validation

            if not validation.get("valid", False):
                issues = validation.get("issues", "unknown")
                suggestions = validation.get("suggestions", "")
                print(f"⚠️  [Iteration {iteration}] Validation failed: {issues}")
                iter_log["action"] = "rewrite (validation)"
                iter_log["validation"] = validation
                result.history.append(iter_log)

                if iteration < max_retries:
                    # Feed validation feedback back to the code-gen LLM
                    fix_msg = (
                        f"The code ran but the output failed validation.\n"
                        f"Issues: {issues}\nSuggestions: {suggestions}\n"
                        f"Please fix the code to address these issues."
                    )
                    code = fix_code(client, query, xml_paths, code, fix_msg, stdout)
                    result.code = code
                    continue
                else:
                    print(f"💀 Max retries reached after validation failure.")
                    break
            else:
                print(f"✅ [Iteration {iteration}] Validation passed!")

        iter_log["action"] = "success"
        iter_log["success"] = True
        result.history.append(iter_log)
        result.success = True
        break

    return result


# ---------------------------------------------------------------------------
# Interactive REPL
# ---------------------------------------------------------------------------
def interactive_loop(client: anthropic.Anthropic, xml_paths: list[str]):
    """Run an interactive session where the user can ask multiple queries."""
    print("\n" + "=" * 60)
    print("  IRS 990 XML Agentic Extractor")
    print("  Type your query, or 'quit' to exit.")
    print("  Example queries:")
    print("    - Find the organization name and EIN")
    print("    - What is the total revenue and total expenses?")
    print("    - List all officer names and their compensation")
    print("    - Find all Schedule A public support data")
    print("    - What is the net assets or fund balance?")
    print("=" * 60)

    while True:
        try:
            query = input("\n🔎 Query > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye!")
            break

        if not query or query.lower() in ("quit", "exit", "q"):
            print("Goodbye!")
            break

        result = run_agent(client, query, xml_paths)
        print(result.summary())

        # Optionally save the generated code
        save = input("\n💾 Save generated code? (y/N) > ").strip().lower()
        if save == "y":
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_query = "".join(c if c.isalnum() else "_" for c in query[:40])
            filename = f"irs990_{safe_query}_{ts}.py"
            with open(filename, "w") as f:
                f.write(result.code)
            print(f"   Saved to {filename}")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Agentic IRS 990 XML data extractor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              python irs990_agent.py --xml-dir ./990_xmls
              python irs990_agent.py --query "Find total revenue" --xml-file form990.xml
              python irs990_agent.py --xml-dir ./data --query "List officer compensation"
        """),
    )
    parser.add_argument("--xml-file", help="Path to a single IRS 990 XML file")
    parser.add_argument("--xml-dir", help="Directory containing IRS 990 XML files")
    parser.add_argument("--query", help="Single query (non-interactive mode)")
    parser.add_argument("--max-retries", type=int, default=MAX_RETRIES,
                        help=f"Max self-correction attempts (default: {MAX_RETRIES})")
    parser.add_argument("--no-validate", action="store_true",
                        help="Skip LLM validation of outputs")
    parser.add_argument("--api-key", help="Anthropic API key (or set ANTHROPIC_API_KEY)")
    args = parser.parse_args()

    # Resolve API key
    api_key = args.api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        sys.exit(
            "Error: Set ANTHROPIC_API_KEY environment variable or pass --api-key.\n"
            "  export ANTHROPIC_API_KEY='sk-ant-...'"
        )

    client = anthropic.Anthropic(api_key=api_key)

    # Resolve XML paths
    xml_paths = resolve_xml_paths(args.xml_file, args.xml_dir)
    if not xml_paths:
        # Allow starting without files in interactive mode
        if not args.query:
            print("⚠️  No XML files found. You can still enter queries but execution will fail.")
            print("   Use --xml-file or --xml-dir to provide IRS 990 XML files.")
            xml_paths = []
        else:
            sys.exit("Error: No XML files found. Use --xml-file or --xml-dir.")

    # Single query or interactive mode
    if args.query:
        result = run_agent(
            client, args.query, xml_paths,
            max_retries=args.max_retries,
            validate=not args.no_validate,
        )
        print(result.summary())

        # Write final code to file for reference
        out_path = os.path.join(SANDBOX_DIR, "final_code.py")
        with open(out_path, "w") as f:
            f.write(result.code)
        print(f"\nGenerated code saved to: {out_path}")
        sys.exit(0 if result.success else 1)
    else:
        interactive_loop(client, xml_paths)


if __name__ == "__main__":
    main()
