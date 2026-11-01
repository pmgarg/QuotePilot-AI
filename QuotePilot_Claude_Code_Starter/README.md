# QuotePilot AI — Claude Code Cloud starter

1. Create a private GitHub repository and commit these files.
2. Open https://claude.ai/code, choose the repository and start a Cloud Session.
3. Paste the complete text of `MASTER_PROMPT.md` into the Claude Code task, or tell Claude to read the committed file and execute it verbatim.
4. Claude must create all-stage design documents, implement Stage 1 first, run QA/security checks, invoke the independent `release-reviewer` subagent and honestly track the completion status of all four stages.
5. Review the code and CI findings yourself before deploying or adding real customer data. Never put actual API keys in the repository.

The `release-reviewer.md` file is a project-scoped read-only reviewer definition. Its existence alone does not mean it was invoked; verify reviewer reports and evidence after each stage.
