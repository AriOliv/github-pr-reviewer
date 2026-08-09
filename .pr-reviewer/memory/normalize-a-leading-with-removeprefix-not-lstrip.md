---
name: normalize-a-leading-with-removeprefix-not-lstrip
title: Normalize a leading ./ with removeprefix, not lstrip
scope: review
tags: correctness,paths
---

To drop a leading './' from a path use str.removeprefix('./'); lstrip('./') strips any leading '.' or '/' char and corrupts hidden paths like .github/ or .pr-reviewer/ into github/ / pr-reviewer/.
