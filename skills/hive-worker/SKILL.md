# Skill: Swarm Worker Gateway

## Overview
Tools for participating in the Swarm swarm as an autonomous worker. These tools connect you to the swarm-gateway for task assignment and result submission.

## Workflow
1. Register with the worker pool
2. Poll for tasks
3. Execute the task using other available skills/tools
4. Send heartbeats during long tasks
5. Submit your completed work
6. Deregister when done

## Tools

### worker_register
Join the worker pool with your capabilities.
```bash
gearcore call swarm-gateway worker_register '{"name": "worker-1", "capabilities": ["code", "research"]}'
```
Save the returned `worker_id` — required for all subsequent calls.

### worker_poll_task
Claim the next available task from the queue.
```bash
gearcore call swarm-gateway worker_poll_task '{"worker_id": "..."}'
```

### worker_heartbeat
Report progress mid-task (for long tasks). If response contains `should_stop: true`, submit partial results and shut down.
```bash
gearcore call swarm-gateway worker_heartbeat '{"worker_id": "...", "status": "working"}'
```

### worker_submit_result
Submit your completed work.
```bash
gearcore call swarm-gateway worker_submit_result '{"worker_id": "...", "task_id": "...", "result": "..."}'
```

### worker_get_messages
Check for operator commands each poll cycle.
```bash
gearcore call swarm-gateway worker_get_messages '{"worker_id": "..."}'
```

### worker_deregister
Leave the pool when done.
```bash
gearcore call swarm-gateway worker_deregister '{"worker_id": "..."}'
```
