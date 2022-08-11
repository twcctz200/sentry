from datetime import timedelta
from typing import Sequence

from sentry import features
from sentry.models import Activity, Group, GroupStatus
from sentry.signals import issue_resolved
from sentry.tasks.base import instrumented_task
from sentry.utils.suspect_resolutions import analytics
from sentry.utils.suspect_resolutions.commit_correlation import is_issue_commit_correlated
from sentry.utils.suspect_resolutions.metric_correlation import is_issue_error_rate_correlated


@issue_resolved.connect(weak=False)
def record_suspect_resolutions(organization_id, project, group, user, resolution_type, **kwargs):
    if features.has("projects:suspect-resolutions", project):
        get_suspect_resolutions.delay(group.id)


@instrumented_task(name="sentry.tasks.get_suspect_resolutions", queue="get_suspect_resolutions")
def get_suspect_resolutions(resolved_issue_id: int) -> Sequence[int]:
    resolved_issue = Group.objects.get(id=resolved_issue_id)
    resolution_type = (
        Activity.objects.filter(group=resolved_issue).values_list("type", flat=True).first()
    )

    if resolved_issue.status != GroupStatus.RESOLVED or resolution_type is None:
        return []

    all_project_issues = list(
        Group.objects.filter(
            status=GroupStatus.UNRESOLVED,
            project=resolved_issue.project,
            last_seen__lte=(resolved_issue.last_seen + timedelta(hours=1)),
            last_seen__gte=(resolved_issue.last_seen - timedelta(hours=1)),
        ).exclude(id=resolved_issue.id)[:100]
    )

    correlated_issue_ids = []

    result = is_issue_error_rate_correlated(resolved_issue, all_project_issues)

    if result is None:
        return correlated_issue_ids

    for metric_correlation_result in result[0]:
        (
            is_commit_correlated,
            resolved_issue_release_ids,
            candidate_issue_release_ids,
        ) = is_issue_commit_correlated(
            resolved_issue.id,
            metric_correlation_result.candidate_suspect_resolution_id,
            resolved_issue.project.id,
        )
        if metric_correlation_result.is_correlated and is_commit_correlated:
            correlated_issue_ids.append(metric_correlation_result.candidate_suspect_resolution_id)
        analytics.record(
            "suspect_resolution.evaluation",
            resolved_group_id=resolved_issue.id,
            candidate_group_id=metric_correlation_result.candidate_suspect_resolution_id,
            resolved_group_resolution_type=resolution_type,
            pearson_r_coefficient=metric_correlation_result.coefficient,
            pearson_r_start_time=result[2],
            pearson_r_end_time=result[3],
            pearson_r_resolution_time=result[1],
            is_commit_correlated=is_commit_correlated,
            resolved_issue_release_ids=resolved_issue_release_ids,
            candidate_issue_release_ids=candidate_issue_release_ids,
        )

    return correlated_issue_ids