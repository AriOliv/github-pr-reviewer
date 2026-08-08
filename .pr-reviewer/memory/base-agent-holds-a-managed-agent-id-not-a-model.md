---
name: base-agent-holds-a-managed-agent-id-not-a-model
title: BASE_AGENT holds a managed-agent id, not a model
scope: review, scan
tags: gemini,config
---

BASE_AGENT selects the Managed Agents agent id (e.g. antigravity-preview-05-2026). A model id like gemini-3.6-flash there returns HTTP 400 ('refers to a model, use the model field'); model ids belong only in the generate_content fallback path.
