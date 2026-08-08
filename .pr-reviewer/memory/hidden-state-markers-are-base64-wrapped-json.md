---
name: hidden-state-markers-are-base64-wrapped-json
title: Hidden state markers are base64-wrapped JSON
scope: review
tags: parsing,convention
---

The reviewer's hidden HTML-comment markers encode their JSON payload as base64 so model-generated text containing --> or braces cannot break the parser. Keep new markers base64-encoded rather than embedding raw JSON.
