---
name: table_summary
version: 1
sent: task   # only the Task section is sent, word for word; {table} is the table text
---
## Role
You summarise a table taken from a financial document in one sentence for a search index.

## Task
Write ONE plain sentence of at most 30 words saying what this table shows. No preamble, no quotes, no bullet.

{table}

## Constraints
One sentence, at most 30 words. Say what the table lists, not what you think of it. Do not repeat the numbers.

## Output format
One plain sentence.

## Good example
Maximum cover limits by plan for each insured location.

## Bad example
Sure! Here is a summary: this very detailed table shows a lot of numbers that are probably important.
This is wrong: it has a preamble, it is not one sentence and it says nothing about the content.

## Refusal rule
If the table is empty or unreadable, write: Table content could not be read.
