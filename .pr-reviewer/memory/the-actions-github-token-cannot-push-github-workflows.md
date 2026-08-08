---
name: the-actions-github-token-cannot-push-github-workflows
title: The Actions GITHUB_TOKEN cannot push .github/workflows/
scope: issue, review
tags: actions,security
---

GitHub blocks the built-in Actions token from creating or updating workflow files; pushing them needs a PAT with the 'workflow' scope. Auto-generating runnable CI is also a security risk (it runs with repo secrets). Generators must skip .github/workflows/** and surface the skip rather than failing.
