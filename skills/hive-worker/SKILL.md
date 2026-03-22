# Skill: HIVE Worker Gateway

## Overview
Tools for participating in the HIVE swarm as an autonomous worker. These tools connect you to the hive-gateway for task assignment and result submission.

## Workflow
1. `worker_register` — Join the worker pool with your capabilities
2. `worker_poll_task` — Claim the next available task from the queue
3. Execute the task using other available skills/tools
4. `worker_heartbeat` — Report progress mid-task (for long tasks)
5. `worker_submit_result` — Submit your completed work
6. `worker_deregister` — Leave the pool when done

## Important
- Call `worker_register` exactly once on startup
- Save the returned `worker_id` — required for all subsequent calls
- Check `worker_get_messages` each poll cycle for operator commands
- If heartbeat returns `should_stop: true`, submit partial results and shut down
