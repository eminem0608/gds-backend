"""Collapse role-specific copies of the same recipe review in a visible task set."""
import re


def unique_review_tasks(tasks, user_id):
    result = []
    positions = {}

    def preference(task):
        return (task.state not in {'done', 'cancelled'}, task.assignee_id.id == user_id)

    for task in tasks:
        if task.task_type != 'recipe_review' or not task.target_id:
            result.append(task)
            continue
        # Review task names carry the submission round. Keep previous rounds
        # separate, including legacy names whose round cannot be determined.
        match = re.search(r' · 第 (\d+) 轮$', task.name or '')
        review_round = int(match.group(1)) if match else task.name
        key = (task.company_id.id, task.target_model, task.target_id, review_round)
        if key not in positions:
            positions[key] = len(result)
            result.append(task)
        elif preference(task) > preference(result[positions[key]]):
            result[positions[key]] = task
    return result
