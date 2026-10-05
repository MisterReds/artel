# Artel doctor

**Бенчмарк:** pass 100%, partial 1.00, $3.1921, 145.0 с на задачу, отказов 0, срывов 0.

**Траектории:** проектов 4 {'done': 4}, стоимость по ролям {'worker': 0.9242, 'planner': 0.7602, 'reviewer': 0.6347, 'auditor': 0.3041, 'librarian': 0.2907, 'refiner': 0.2781}.

## Находки

- **[medium] cost: coordination roles take 71% of spend** — {"worker": 0.9242, "planner": 0.7602, "reviewer": 0.6347, "auditor": 0.3041, "librarian": 0.2907, "refiner": 0.2781}
- **[low] tools: tool calls returned errors** — {"task_submit": ["missing required arguments: ['evidence']", "missing required arguments: ['evidence']", "missing required arguments: ['evidence']"], "review_verdict": ["missing required arguments: ['passed']", "missing required arguments: ['passed']", "missing required arguments: ['passed']"], "Bash": ["Exit code 1\n.:\ntotal 272\ndrwxr-xr-x 6 root root   4096 Oct  5 05:49 .\ndrwxr-xr-x 3 root ro
