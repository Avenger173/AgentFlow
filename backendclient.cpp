#include "backendclient.h"

#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QNetworkReply>
#include <QNetworkRequest>
#include <QSet>
#include <QUrlQuery>

namespace {

QString replyErrorMessage(QNetworkReply *reply)
{
    // Qt 的 errorString 通常只会给出英文 HTTP 描述。FastAPI 422 的响应体包含字段级原因，
    // 在错误路径读取并压缩它，客户才知道是“多选超限”还是“问题描述过长”，而不是面对
    // "Unprocessable Content" 猜测发生了什么。
    const QString errorText = reply->errorString();
    const int statusCode = reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt();
    QString serverDetail;
    const QJsonDocument response = QJsonDocument::fromJson(reply->readAll());
    if (response.isObject()) {
        const QJsonValue detail = response.object().value(QStringLiteral("detail"));
        if (detail.isString()) {
            serverDetail = detail.toString().trimmed();
        } else if (detail.isArray()) {
            const QJsonArray violations = detail.toArray();
            if (!violations.isEmpty() && violations.first().isObject()) {
                const QJsonObject violation = violations.first().toObject();
                const QJsonArray location = violation.value(QStringLiteral("loc")).toArray();
                const QString field = location.isEmpty() ? QString() : location.last().toString();
                const QString type = violation.value(QStringLiteral("type")).toString();
                if (field == QStringLiteral("document_refs") && type.contains(QStringLiteral("max_length"))) {
                    serverDetail = QStringLiteral("一次多文档对比最多选择 4 份材料。");
                } else if (field == QStringLiteral("task_goal") && type.contains(QStringLiteral("max_length"))) {
                    serverDetail = QStringLiteral("问题描述过长，请控制在 2000 个字符以内。");
                } else if (!field.isEmpty()) {
                    serverDetail = QStringLiteral("字段“%1”不符合要求：%2")
                                       .arg(field, violation.value(QStringLiteral("msg")).toString());
                }
            }
        }
    }
    if (statusCode > 0) {
        return QString("HTTP %1 · %2").arg(statusCode).arg(
            serverDetail.isEmpty() ? errorText : serverDetail);
    }
    return serverDetail.isEmpty() ? errorText : serverDetail;
}

QStringList readStringList(const QJsonArray &array)
{
    QStringList values;
    values.reserve(array.size());
    for (const QJsonValue &value : array) {
        values.append(value.toString());
    }
    return values;
}

McpConnectionInfo readMcpConnectionInfo(const QJsonObject &payload)
{
    McpConnectionInfo result;
    result.connectionId = payload.value(QStringLiteral("connection_id")).toString();
    result.displayName = payload.value(QStringLiteral("display_name")).toString();
    result.description = payload.value(QStringLiteral("description")).toString();
    result.transport = payload.value(QStringLiteral("transport")).toString();
    result.status = payload.value(QStringLiteral("status")).toString();
    result.enabled = payload.value(QStringLiteral("enabled")).toBool(false);
    result.requiresNetwork = payload.value(QStringLiteral("requires_network")).toBool(false);
    result.requiresCommandConfirmation = payload.value(QStringLiteral("requires_command_confirmation")).toBool(false);
    result.originSummary = payload.value(QStringLiteral("origin_summary")).toString();
    result.lastCheckedAt = payload.value(QStringLiteral("last_checked_at")).toString();
    result.lastToolCount = payload.value(QStringLiteral("last_tool_count")).toInt();
    result.lastErrorCode = payload.value(QStringLiteral("last_error_code")).toString();
    return result;
}

ConversationContextInfo readConversationContextInfo(const QJsonObject &payload)
{
    // 后端的 ConversationContext 包含完整 session 元数据；Qt 只消费恢复界面所需的最小字段，
    // 以免未来往 session 扩展内部审计字段时意外暴露到对话页。
    ConversationContextInfo result;
    const QJsonObject session = payload.value(QStringLiteral("session")).toObject();
    result.conversationId = session.value(QStringLiteral("conversation_id")).toString();
    result.projectScope = session.value(QStringLiteral("project_scope")).toString();
    result.title = session.value(QStringLiteral("title")).toString();
    result.summary = session.value(QStringLiteral("summary")).toString();

    const QJsonArray messages = payload.value(QStringLiteral("recent_messages")).toArray();
    result.recentMessages.reserve(messages.size());
    for (const QJsonValue &value : messages) {
        const QJsonObject item = value.toObject();
        const QString role = item.value(QStringLiteral("role")).toString();
        const QString content = item.value(QStringLiteral("content")).toString();
        if ((role != QStringLiteral("user") && role != QStringLiteral("assistant")) || content.isEmpty()) {
            continue;
        }
        result.recentMessages.append(ConversationTranscriptMessage{role, content});
    }
    return result;
}

ConversationSessionListResult readConversationSessionListResult(const QJsonObject &payload)
{
    ConversationSessionListResult result;
    result.projectScope = payload.value(QStringLiteral("project_scope")).toString();
    const QJsonArray conversations = payload.value(QStringLiteral("conversations")).toArray();
    result.conversations.reserve(conversations.size());
    for (const QJsonValue &value : conversations) {
        if (!value.isObject()) {
            continue;
        }
        const QJsonObject item = value.toObject();
        ConversationSessionInfo session;
        session.conversationId = item.value(QStringLiteral("conversation_id")).toString();
        session.projectScope = item.value(QStringLiteral("project_scope")).toString();
        session.title = item.value(QStringLiteral("title")).toString();
        session.summary = item.value(QStringLiteral("summary")).toString();
        session.archivedMessageCount = item.value(QStringLiteral("archived_message_count")).toInt();
        session.updatedAt = item.value(QStringLiteral("updated_at")).toString();
        if (!session.conversationId.isEmpty()) {
            result.conversations.append(session);
        }
    }
    return result;
}

ConversationTranscriptPageResult readConversationTranscriptPageResult(const QJsonObject &payload)
{
    ConversationTranscriptPageResult result;
    const QJsonObject session = payload.value(QStringLiteral("session")).toObject();
    result.session.conversationId = session.value(QStringLiteral("conversation_id")).toString();
    result.session.projectScope = session.value(QStringLiteral("project_scope")).toString();
    result.session.title = session.value(QStringLiteral("title")).toString();
    result.session.summary = session.value(QStringLiteral("summary")).toString();
    result.session.archivedMessageCount = session.value(QStringLiteral("archived_message_count")).toInt();
    result.session.updatedAt = session.value(QStringLiteral("updated_at")).toString();
    result.offset = payload.value(QStringLiteral("offset")).toInt();
    result.limit = payload.value(QStringLiteral("limit")).toInt();
    result.total = payload.value(QStringLiteral("total")).toInt();
    const QJsonArray messages = payload.value(QStringLiteral("messages")).toArray();
    result.messages.reserve(messages.size());
    for (const QJsonValue &value : messages) {
        if (!value.isObject()) {
            continue;
        }
        const QJsonObject item = value.toObject();
        ConversationTranscriptMessage message;
        message.role = item.value(QStringLiteral("role")).toString();
        message.content = item.value(QStringLiteral("content")).toString();
        if (!message.role.isEmpty() && !message.content.isEmpty()) {
            result.messages.append(message);
        }
    }
    return result;
}

QList<WorkflowStepInfo> readWorkflowSteps(const QJsonObject &workflowPlan)
{
    // 后端 Pydantic 使用 depends_on，Qt 端使用 dependsOn。
    // 字段转换集中在这里，避免 UI 层散落 JSON 字段名。
    QList<WorkflowStepInfo> steps;
    const QJsonArray stepsArray = workflowPlan.value(QStringLiteral("steps")).toArray();
    steps.reserve(stepsArray.size());

    for (const QJsonValue &value : stepsArray) {
        const QJsonObject object = value.toObject();
        WorkflowStepInfo step;
        step.id = object.value(QStringLiteral("id")).toString();
        step.agent = object.value(QStringLiteral("agent")).toString();
        step.action = object.value(QStringLiteral("action")).toString();
        step.title = object.value(QStringLiteral("title")).toString();
        step.dependsOn = readStringList(object.value(QStringLiteral("depends_on")).toArray());
        step.parallelGroup = object.value(QStringLiteral("parallel_group")).toString();
        step.input = object.value(QStringLiteral("input")).toObject();
        step.reason = object.value(QStringLiteral("reason")).toString();
        step.expectedOutput = object.value(QStringLiteral("expected_output")).toString();
        step.requiredPermissions = readStringList(object.value(QStringLiteral("required_permissions")).toArray());
        step.riskLevel = object.value(QStringLiteral("risk_level")).toString(QStringLiteral("low"));
        step.requiresConfirmation = object.value(QStringLiteral("requires_confirmation")).toBool();
        step.toolName = object.value(QStringLiteral("tool_name")).toString();
        step.commandPolicy = object.value(QStringLiteral("command_policy")).toObject();
        step.successCriteria = readStringList(object.value(QStringLiteral("success_criteria")).toArray());
        step.timeoutMs = object.value(QStringLiteral("timeout_ms")).toInt();
        step.retryPolicy = object.value(QStringLiteral("retry_policy")).toObject();
        step.executionMode = object.value(QStringLiteral("execution_mode")).toString(QStringLiteral("execute"));
        step.admissionStatus = object.value(QStringLiteral("admission_status")).toString(QStringLiteral("ready"));
        step.admissionReason = object.value(QStringLiteral("admission_reason")).toString();
        step.verificationScope = object.value(QStringLiteral("verification_scope")).toString();
        step.recoveryHint = object.value(QStringLiteral("recovery_hint")).toString();
        steps.append(step);
    }

    return steps;
}

WorkflowBudgetEstimateInfo readWorkflowBudgetEstimateInfo(const QJsonObject &payload)
{
    WorkflowBudgetEstimateInfo info;
    info.stepCount = payload.value(QStringLiteral("step_count")).toInt();
    info.timeLevel = payload.value(QStringLiteral("time_level")).toString();
    info.modelCostLevel = payload.value(QStringLiteral("model_cost_level")).toString();
    info.requiresNetwork = payload.value(QStringLiteral("requires_network")).toBool();
    info.requiresCommand = payload.value(QStringLiteral("requires_command")).toBool();
    return info;
}

WorkflowWorkspaceScopeInfo readWorkflowWorkspaceScopeInfo(const QJsonObject &payload)
{
    WorkflowWorkspaceScopeInfo info;
    info.readPaths = readStringList(payload.value(QStringLiteral("read_paths")).toArray());
    info.writePaths = readStringList(payload.value(QStringLiteral("write_paths")).toArray());
    info.externalServices = readStringList(payload.value(QStringLiteral("external_services")).toArray());
    info.notes = payload.value(QStringLiteral("notes")).toString();
    return info;
}

WorkflowPlanSummaryInfo readWorkflowPlanSummaryInfo(const QJsonObject &workflowPlan)
{
    WorkflowPlanSummaryInfo info;
    info.schemaVersion = workflowPlan.value(QStringLiteral("schema_version")).toString();
    info.planId = workflowPlan.value(QStringLiteral("plan_id")).toString();
    info.planVersion = workflowPlan.value(QStringLiteral("plan_version")).toInt();
    info.parentPlanId = workflowPlan.value(QStringLiteral("parent_plan_id")).toString();
    info.userGoal = workflowPlan.value(QStringLiteral("user_goal")).toString();
    info.changeSummary = workflowPlan.value(QStringLiteral("change_summary")).toString();
    info.intent = workflowPlan.value(QStringLiteral("intent")).toString();
    info.summary = workflowPlan.value(QStringLiteral("summary")).toString();
    info.nextAction = workflowPlan.value(QStringLiteral("next_action")).toString();
    info.executionReadiness = workflowPlan.value(QStringLiteral("execution_readiness"))
                                  .toString(info.executionReadiness);
    info.projectScope = workflowPlan.value(QStringLiteral("project_scope"))
                            .toString(info.projectScope);
    const QJsonArray agentHints = workflowPlan.value(QStringLiteral("agent_hints")).toArray();
    for (const QJsonValue &value : agentHints) {
        const QString agentId = value.toObject().value(QStringLiteral("agent_id")).toString();
        if (!agentId.isEmpty() && !info.agentHints.contains(agentId)) {
            info.agentHints.append(agentId);
        }
    }
    info.clarifyingQuestions = readStringList(workflowPlan.value(QStringLiteral("clarifying_questions")).toArray());
    info.definitionOfDone = readStringList(workflowPlan.value(QStringLiteral("definition_of_done")).toArray());

    // 偏好是计划生成时的审计快照；缺字段时保留 C++ 默认值，兼容较早的历史任务。
    const QJsonObject preferences = workflowPlan.value(QStringLiteral("preference_applied")).toObject();
    info.preferences.permissionPolicy = preferences.value(QStringLiteral("permission_policy"))
                                            .toString(info.preferences.permissionPolicy);
    info.preferences.personality = preferences.value(QStringLiteral("personality"))
                                       .toString(info.preferences.personality);
    info.preferences.costMode = preferences.value(QStringLiteral("cost_mode"))
                                    .toString(info.preferences.costMode);
    info.preferences.executionStyle = preferences.value(QStringLiteral("execution_style"))
                                          .toString(info.preferences.executionStyle);
    info.preferences.detailLevel = preferences.value(QStringLiteral("detail_level"))
                                       .toString(info.preferences.detailLevel);
    info.preferences.memoryEnabled = preferences.value(QStringLiteral("memory_enabled"))
                                         .toBool(info.preferences.memoryEnabled);
    info.budgetEstimate = readWorkflowBudgetEstimateInfo(workflowPlan.value(QStringLiteral("budget_estimate")).toObject());
    info.workspaceScope = readWorkflowWorkspaceScopeInfo(workflowPlan.value(QStringLiteral("workspace_scope")).toObject());
    return info;
}

WorkflowPlanDetailResult readWorkflowPlanDetailResult(const QJsonObject &payload)
{
    WorkflowPlanDetailResult result;
    result.taskId = payload.value(QStringLiteral("task_id")).toString();
    const QJsonObject workflowPlan = payload.value(QStringLiteral("workflow_plan")).toObject();
    result.planSummary = readWorkflowPlanSummaryInfo(workflowPlan);
    result.steps = readWorkflowSteps(workflowPlan);
    return result;
}

WorkflowPlanVersionInfo readWorkflowPlanVersionInfo(const QJsonObject &payload)
{
    WorkflowPlanVersionInfo info;
    info.taskId = payload.value(QStringLiteral("task_id")).toString();
    info.planId = payload.value(QStringLiteral("plan_id")).toString();
    info.planVersion = payload.value(QStringLiteral("plan_version")).toInt();
    info.parentPlanId = payload.value(QStringLiteral("parent_plan_id")).toString();
    info.userGoal = payload.value(QStringLiteral("user_goal")).toString();
    info.changeSummary = payload.value(QStringLiteral("change_summary")).toString();
    info.createdAt = payload.value(QStringLiteral("created_at")).toString();
    info.current = payload.value(QStringLiteral("is_current")).toBool();
    return info;
}

WorkflowPlanVersionListResult readWorkflowPlanVersionListResult(const QJsonObject &payload)
{
    WorkflowPlanVersionListResult result;
    result.taskId = payload.value(QStringLiteral("task_id")).toString();
    result.currentPlanId = payload.value(QStringLiteral("current_plan_id")).toString();
    const QJsonArray versions = payload.value(QStringLiteral("versions")).toArray();
    result.versions.reserve(versions.size());
    for (const QJsonValue &value : versions) {
        const WorkflowPlanVersionInfo version = readWorkflowPlanVersionInfo(value.toObject());
        if (version.planVersion > 0 && !version.planId.isEmpty()) {
            result.versions.append(version);
        }
    }
    return result;
}

WorkflowPlanRevisionResult readWorkflowPlanRevisionResult(const QJsonObject &payload)
{
    WorkflowPlanRevisionResult result;
    result.taskId = payload.value(QStringLiteral("task_id")).toString();
    result.message = payload.value(QStringLiteral("message")).toString();
    const QJsonObject workflowRun = payload.value(QStringLiteral("workflow_run")).toObject();
    result.workflowRunStatus = workflowRun.value(QStringLiteral("status")).toString();
    const QJsonObject workflowPlan = payload.value(QStringLiteral("workflow_plan")).toObject();
    result.planSummary = readWorkflowPlanSummaryInfo(workflowPlan);
    result.steps = readWorkflowSteps(workflowPlan);
    return result;
}

WorkspaceDocumentInfo readWorkspaceDocumentInfo(const QJsonObject &payload)
{
    WorkspaceDocumentInfo document;
    document.name = payload.value(QStringLiteral("name")).toString();
    document.relativePath = payload.value(QStringLiteral("relative_path")).toString();
    document.sizeBytes = payload.value(QStringLiteral("size_bytes")).toInt();
    document.modifiedAt = payload.value(QStringLiteral("modified_at")).toString();
    document.documentType = payload.value(QStringLiteral("document_type")).toString(QStringLiteral("text"));
    document.preview = payload.value(QStringLiteral("preview")).toString();
    return document;
}

WorkspaceDocumentListResult readWorkspaceDocumentListResult(const QJsonObject &payload)
{
    WorkspaceDocumentListResult result;
    result.total = payload.value(QStringLiteral("total")).toInt();
    const QJsonArray documents = payload.value(QStringLiteral("documents")).toArray();
    result.documents.reserve(documents.size());
    for (const QJsonValue &value : documents) {
        if (value.isObject()) {
            result.documents.append(readWorkspaceDocumentInfo(value.toObject()));
        }
    }
    return result;
}

KnowledgeBaseInfo readKnowledgeBaseInfo(const QJsonObject &payload)
{
    KnowledgeBaseInfo knowledgeBase;
    knowledgeBase.knowledgeBaseId = payload.value(QStringLiteral("knowledge_base_id")).toString();
    knowledgeBase.name = payload.value(QStringLiteral("name")).toString();
    knowledgeBase.description = payload.value(QStringLiteral("description")).toString();
    knowledgeBase.status = payload.value(QStringLiteral("status")).toString();
    knowledgeBase.activeIndexGeneration = payload.value(QStringLiteral("active_index_generation")).toInt();
    knowledgeBase.activeDocumentVersionCount = payload.value(QStringLiteral("active_document_version_count")).toInt();
    knowledgeBase.updatedAt = payload.value(QStringLiteral("updated_at")).toString();
    return knowledgeBase;
}

KnowledgeBaseListResult readKnowledgeBaseListResult(const QJsonObject &payload)
{
    KnowledgeBaseListResult result;
    const QJsonArray knowledgeBases = payload.value(QStringLiteral("knowledge_bases")).toArray();
    result.knowledgeBases.reserve(knowledgeBases.size());
    for (const QJsonValue &value : knowledgeBases) {
        if (value.isObject()) {
            result.knowledgeBases.append(readKnowledgeBaseInfo(value.toObject()));
        }
    }
    return result;
}

KnowledgeDocumentInfo readKnowledgeDocumentInfo(const QJsonObject &payload)
{
    KnowledgeDocumentInfo document;
    document.documentId = payload.value(QStringLiteral("document_id")).toString();
    document.knowledgeBaseId = payload.value(QStringLiteral("knowledge_base_id")).toString();
    document.displayName = payload.value(QStringLiteral("display_name")).toString();
    document.documentType = payload.value(QStringLiteral("document_type")).toString();
    document.activeVersionId = payload.value(QStringLiteral("active_version_id")).toString();
    document.activeVersionStatus = payload.value(QStringLiteral("active_version_status")).toString();
    document.activeOcrPageCount = payload.value(QStringLiteral("active_ocr_page_count")).toInt();
    document.activeOcrCompletedPageCount = payload.value(QStringLiteral("active_ocr_completed_page_count")).toInt();
    document.activeOcrFailedPageCount = payload.value(QStringLiteral("active_ocr_failed_page_count")).toInt();
    document.activeOcrRetriedPageCount = payload.value(QStringLiteral("active_ocr_retried_page_count")).toInt();
    document.activeFailureSummary = payload.value(QStringLiteral("active_failure_summary")).toString();
    document.updatedAt = payload.value(QStringLiteral("updated_at")).toString();
    return document;
}

KnowledgeDocumentListResult readKnowledgeDocumentListResult(const QJsonObject &payload)
{
    KnowledgeDocumentListResult result;
    result.knowledgeBaseId = payload.value(QStringLiteral("knowledge_base_id")).toString();
    const QJsonArray documents = payload.value(QStringLiteral("documents")).toArray();
    result.documents.reserve(documents.size());
    for (const QJsonValue &value : documents) {
        if (value.isObject()) {
            result.documents.append(readKnowledgeDocumentInfo(value.toObject()));
        }
    }
    return result;
}

KnowledgeIndexJobInfo readKnowledgeIndexJobInfo(const QJsonObject &payload)
{
    KnowledgeIndexJobInfo job;
    job.indexJobId = payload.value(QStringLiteral("index_job_id")).toString();
    job.knowledgeBaseId = payload.value(QStringLiteral("knowledge_base_id")).toString();
    job.status = payload.value(QStringLiteral("status")).toString();
    job.stage = payload.value(QStringLiteral("stage")).toString();
    job.totalDocumentCount = payload.value(QStringLiteral("total_document_count")).toInt();
    job.parsedDocumentCount = payload.value(QStringLiteral("parsed_document_count")).toInt();
    job.indexedDocumentCount = payload.value(QStringLiteral("indexed_document_count")).toInt();
    job.failedDocumentCount = payload.value(QStringLiteral("failed_document_count")).toInt();
    const QJsonArray failures = payload.value(QStringLiteral("failure_summaries")).toArray();
    for (const QJsonValue &value : failures) {
        if (value.isString()) {
            job.failureSummaries.append(value.toString());
        }
    }
    return job;
}

KnowledgeVectorCapabilityInfo readKnowledgeVectorCapabilityInfo(const QJsonObject &payload)
{
    KnowledgeVectorCapabilityInfo capability;
    capability.chromaAvailable = payload.value(QStringLiteral("chroma_available")).toBool();
    capability.fastembedAvailable = payload.value(QStringLiteral("fastembed_available")).toBool();
    capability.modelInitialized = payload.value(QStringLiteral("model_initialized")).toBool();
    capability.message = payload.value(QStringLiteral("message")).toString();
    return capability;
}

KnowledgeOcrCapabilityInfo readKnowledgeOcrCapabilityInfo(const QJsonObject &payload)
{
    KnowledgeOcrCapabilityInfo capability;
    capability.paddleocrAvailable = payload.value(QStringLiteral("paddleocr_available")).toBool();
    capability.modelInitialized = payload.value(QStringLiteral("model_initialized")).toBool();
    capability.profile = payload.value(QStringLiteral("profile")).toString();
    capability.message = payload.value(QStringLiteral("message")).toString();
    return capability;
}

KnowledgeOcrPreparationInfo readKnowledgeOcrPreparationInfo(const QJsonObject &payload)
{
    KnowledgeOcrPreparationInfo preparation;
    preparation.preparationId = payload.value(QStringLiteral("preparation_id")).toString();
    preparation.status = payload.value(QStringLiteral("status")).toString();
    preparation.modelProfile = payload.value(QStringLiteral("model_profile")).toString();
    preparation.message = payload.value(QStringLiteral("message")).toString();
    preparation.startedAt = payload.value(QStringLiteral("started_at")).toString();
    preparation.completedAt = payload.value(QStringLiteral("completed_at")).toString();
    return preparation;
}

KnowledgeAnswerTaskStartResult readKnowledgeAnswerTaskStartResult(const QJsonObject &payload)
{
    KnowledgeAnswerTaskStartResult result;
    result.taskId = payload.value(QStringLiteral("task_id")).toString();
    result.status = payload.value(QStringLiteral("status")).toString();
    return result;
}

KnowledgeAnswerTaskResult readKnowledgeAnswerTaskResult(const QJsonObject &payload)
{
    KnowledgeAnswerTaskResult result;
    result.taskId = payload.value(QStringLiteral("task_id")).toString();
    result.status = payload.value(QStringLiteral("status")).toString();
    result.summary = payload.value(QStringLiteral("summary")).toString();
    result.message = payload.value(QStringLiteral("message")).toString();
    result.result = payload.value(QStringLiteral("result")).toObject();
    return result;
}

KnowledgeDeepTaskStartResult readKnowledgeDeepTaskStartResult(const QJsonObject &payload)
{
    KnowledgeDeepTaskStartResult result;
    result.taskId = payload.value(QStringLiteral("task_id")).toString();
    result.status = payload.value(QStringLiteral("status")).toString();
    return result;
}

KnowledgeDeepTaskResult readKnowledgeDeepTaskResult(const QJsonObject &payload)
{
    KnowledgeDeepTaskResult result;
    result.taskId = payload.value(QStringLiteral("task_id")).toString();
    result.status = payload.value(QStringLiteral("status")).toString();
    result.summary = payload.value(QStringLiteral("summary")).toString();
    result.scope = payload.value(QStringLiteral("scope")).toObject();
    result.result = payload.value(QStringLiteral("result")).toObject();
    result.coverage = payload.value(QStringLiteral("coverage")).toObject();
    result.reportReadiness = payload.value(QStringLiteral("report_readiness")).toObject();
    return result;
}

KnowledgeDeepTaskControlResult readKnowledgeDeepTaskControlResult(const QJsonObject &payload)
{
    KnowledgeDeepTaskControlResult result;
    result.taskId = payload.value(QStringLiteral("task_id")).toString();
    result.action = payload.value(QStringLiteral("action")).toString();
    result.accepted = payload.value(QStringLiteral("accepted")).toBool();
    result.status = payload.value(QStringLiteral("status")).toString();
    result.message = payload.value(QStringLiteral("message")).toString();
    return result;
}

KnowledgeDeepTaskReportExportResult readKnowledgeDeepTaskReportExportResult(const QJsonObject &payload)
{
    KnowledgeDeepTaskReportExportResult result;
    result.taskId = payload.value(QStringLiteral("task_id")).toString();
    result.artifactId = payload.value(QStringLiteral("artifact_id")).toString();
    result.filename = payload.value(QStringLiteral("filename")).toString();
    result.relativePath = payload.value(QStringLiteral("relative_path")).toString();
    result.artifactUri = payload.value(QStringLiteral("artifact_uri")).toString();
    result.characterCount = payload.value(QStringLiteral("character_count")).toInt();
    result.message = payload.value(QStringLiteral("message")).toString();
    return result;
}

DataDatasetInfo readDataDatasetInfo(const QJsonObject &payload)
{
    DataDatasetInfo dataset;
    dataset.name = payload.value(QStringLiteral("name")).toString();
    dataset.relativePath = payload.value(QStringLiteral("relative_path")).toString();
    dataset.sizeBytes = payload.value(QStringLiteral("size_bytes")).toInt();
    dataset.modifiedAt = payload.value(QStringLiteral("modified_at")).toString();
    dataset.datasetType = payload.value(QStringLiteral("dataset_type")).toString();
    return dataset;
}

DataDatasetListResult readDataDatasetListResult(const QJsonObject &payload)
{
    DataDatasetListResult result;
    result.total = payload.value(QStringLiteral("total")).toInt();
    const QJsonArray datasets = payload.value(QStringLiteral("datasets")).toArray();
    result.datasets.reserve(datasets.size());
    for (const QJsonValue &value : datasets) {
        if (value.isObject()) {
            result.datasets.append(readDataDatasetInfo(value.toObject()));
        }
    }
    return result;
}

DocumentAgentRunResult readDocumentAgentRunResult(const QJsonObject &payload)
{
    DocumentAgentRunResult result;
    result.taskId = payload.value(QStringLiteral("task_id")).toString();
    result.mode = payload.value(QStringLiteral("mode")).toString();
    result.status = payload.value(QStringLiteral("status")).toString();
    result.stopReason = payload.value(QStringLiteral("stop_reason")).toString();
    result.reply = payload.value(QStringLiteral("reply")).toString();
    result.documentContext = payload.value(QStringLiteral("document_context")).toObject();
    return result;
}

DocumentAgentTaskStartResult readDocumentAgentTaskStartResult(const QJsonObject &payload)
{
    DocumentAgentTaskStartResult result;
    result.taskId = payload.value(QStringLiteral("task_id")).toString();
    result.status = payload.value(QStringLiteral("status")).toString();
    return result;
}

PdfProcessingTaskStartResult readPdfProcessingTaskStartResult(const QJsonObject &payload)
{
    PdfProcessingTaskStartResult result;
    result.taskId = payload.value(QStringLiteral("task_id")).toString();
    result.status = payload.value(QStringLiteral("status")).toString();
    return result;
}

DocumentDraftSaveResult readDocumentDraftSaveResult(const QJsonObject &payload)
{
    DocumentDraftSaveResult result;
    result.taskId = payload.value(QStringLiteral("task_id")).toString();
    result.artifactId = payload.value(QStringLiteral("artifact_id")).toString();
    result.filename = payload.value(QStringLiteral("filename")).toString();
    result.relativePath = payload.value(QStringLiteral("relative_path")).toString();
    result.artifactUri = payload.value(QStringLiteral("artifact_uri")).toString();
    result.message = payload.value(QStringLiteral("message")).toString();
    return result;
}

PresentationPreviewResult readPresentationPreviewResult(const QJsonObject &payload)
{
    PresentationPreviewResult result;
    result.sourceTaskId = payload.value(QStringLiteral("source_task_id")).toString();
    result.sourceVersionId = payload.value(QStringLiteral("source_version_id")).toString();
    result.presentationType = payload.value(QStringLiteral("presentation_type")).toString();
    result.planId = payload.value(QStringLiteral("plan_id")).toString();
    result.title = payload.value(QStringLiteral("title")).toString();
    result.slides = payload.value(QStringLiteral("slides")).toArray();
    result.preflight = payload.value(QStringLiteral("preflight")).toObject();
    const QJsonArray warnings = payload.value(QStringLiteral("warnings")).toArray();
    for (const QJsonValue &warning : warnings) {
        if (warning.isString()) {
            result.warnings.append(warning.toString());
        }
    }
    return result;
}

PresentationExportResult readPresentationExportResult(const QJsonObject &payload)
{
    PresentationExportResult result;
    result.taskId = payload.value(QStringLiteral("task_id")).toString();
    result.artifactId = payload.value(QStringLiteral("artifact_id")).toString();
    result.filename = payload.value(QStringLiteral("filename")).toString();
    result.relativePath = payload.value(QStringLiteral("relative_path")).toString();
    result.artifactUri = payload.value(QStringLiteral("artifact_uri")).toString();
    result.slideCount = payload.value(QStringLiteral("slide_count")).toInt();
    result.verification = payload.value(QStringLiteral("verification")).toObject();
    result.message = payload.value(QStringLiteral("message")).toString();
    return result;
}

PresentationStudioTaskStartResult readPresentationStudioTaskStartResult(const QJsonObject &payload)
{
    PresentationStudioTaskStartResult result;
    result.taskId = payload.value(QStringLiteral("task_id")).toString();
    result.status = payload.value(QStringLiteral("status")).toString();
    return result;
}

PresentationStudioPlanResult readPresentationStudioPlanResult(const QJsonObject &payload)
{
    PresentationStudioPlanResult result;
    result.taskId = payload.value(QStringLiteral("task_id")).toString();
    result.planId = payload.value(QStringLiteral("plan_id")).toString();
    result.mode = payload.value(QStringLiteral("mode")).toString();
    result.brief = payload.value(QStringLiteral("brief")).toObject();
    result.slides = payload.value(QStringLiteral("slides")).toArray();
    result.assetPlan = payload.value(QStringLiteral("asset_plan")).toObject();
    result.researchPlan = payload.value(QStringLiteral("research_plan")).toObject();
    result.dataPlan = payload.value(QStringLiteral("data_plan")).toObject();
    result.warnings = readStringList(payload.value(QStringLiteral("warnings")).toArray());
    return result;
}

ProjectReviewResult readProjectReviewResult(const QJsonObject &payload)
{
    ProjectReviewResult result;
    result.taskId = payload.value(QStringLiteral("task_id")).toString();
    result.status = payload.value(QStringLiteral("status")).toString();
    result.report = payload.value(QStringLiteral("report")).toObject();
    return result;
}

PaperReviewResult readPaperReviewResult(const QJsonObject &payload)
{
    PaperReviewResult result;
    result.taskId = payload.value(QStringLiteral("task_id")).toString();
    result.status = payload.value(QStringLiteral("status")).toString();
    result.report = payload.value(QStringLiteral("report")).toObject();
    return result;
}

TaskLogEvent readTaskLogEvent(const QJsonObject &payload)
{
    TaskLogEvent event;
    event.taskId = payload.value(QStringLiteral("task_id")).toString();
    event.sequence = payload.value(QStringLiteral("sequence")).toInt();
    event.event = payload.value(QStringLiteral("event")).toString();
    event.agentId = payload.value(QStringLiteral("agent_id")).toString();
    event.stepId = payload.value(QStringLiteral("step_id")).toString();
    event.level = payload.value(QStringLiteral("level")).toString(QStringLiteral("info"));
    event.message = payload.value(QStringLiteral("message")).toString();
    event.createdAt = payload.value(QStringLiteral("created_at")).toString();
    return event;
}

WorkflowTaskUpdateInfo readWorkflowTaskUpdateInfo(const QJsonObject &payload)
{
    WorkflowTaskUpdateInfo update;
    update.sequence = payload.value(QStringLiteral("sequence")).toInt();
    update.updateType = payload.value(QStringLiteral("update_type")).toString();
    update.event = payload.value(QStringLiteral("event")).toString();
    update.level = payload.value(QStringLiteral("level")).toString(QStringLiteral("info"));
    update.agentId = payload.value(QStringLiteral("agent_id")).toString();
    update.stepId = payload.value(QStringLiteral("step_id")).toString();
    update.status = payload.value(QStringLiteral("status")).toString();
    update.title = payload.value(QStringLiteral("title")).toString();
    update.message = payload.value(QStringLiteral("message")).toString();
    update.occurredAt = payload.value(QStringLiteral("occurred_at")).toString();
    update.payload = payload.value(QStringLiteral("payload")).toObject();
    return update;
}

WorkflowTaskUpdateListResult readWorkflowTaskUpdateListResult(const QJsonObject &payload)
{
    WorkflowTaskUpdateListResult result;
    result.taskId = payload.value(QStringLiteral("task_id")).toString();
    result.total = payload.value(QStringLiteral("total")).toInt();

    const QJsonArray updatesArray = payload.value(QStringLiteral("updates")).toArray();
    result.updates.reserve(updatesArray.size());
    for (const QJsonValue &value : updatesArray) {
        const WorkflowTaskUpdateInfo update = readWorkflowTaskUpdateInfo(value.toObject());
        if (!update.event.isEmpty() && !update.message.isEmpty()) {
            result.updates.append(update);
        }
    }

    return result;
}

RuntimePermissionItem readRuntimePermissionItem(const QJsonObject &payload)
{
    RuntimePermissionItem item;

    const QJsonObject requestObject = payload.value(QStringLiteral("request")).toObject();
    const QJsonObject decisionObject = payload.value(QStringLiteral("decision")).toObject();

    item.request.requestId = requestObject.value(QStringLiteral("request_id")).toString();
    item.request.taskId = requestObject.value(QStringLiteral("task_id")).toString();
    item.request.stepId = requestObject.value(QStringLiteral("step_id")).toString();
    item.request.agentId = requestObject.value(QStringLiteral("agent_id")).toString();
    item.request.permissions = readStringList(requestObject.value(QStringLiteral("permissions")).toArray());
    item.request.riskLevel = requestObject.value(QStringLiteral("risk_level")).toString();
    item.request.summary = requestObject.value(QStringLiteral("summary")).toString();
    item.request.details = requestObject.value(QStringLiteral("details")).toObject();
    // 策略字段位于 details 中，以便后端继续兼容早期权限请求协议。
    item.request.permissionPolicy = item.request.details.value(QStringLiteral("permission_policy")).toString();
    item.request.policyAction = item.request.details.value(QStringLiteral("policy_action")).toString();
    item.request.policyReason = item.request.details.value(QStringLiteral("policy_reason")).toString();
    item.request.createdAt = requestObject.value(QStringLiteral("created_at")).toString();

    item.decision.requestId = decisionObject.value(QStringLiteral("request_id")).toString();
    item.decision.taskId = decisionObject.value(QStringLiteral("task_id")).toString();
    item.decision.stepId = decisionObject.value(QStringLiteral("step_id")).toString();
    item.decision.decision = decisionObject.value(QStringLiteral("decision")).toString(QStringLiteral("pending"));
    item.decision.decidedBy = decisionObject.value(QStringLiteral("decided_by")).toString();
    item.decision.decidedAt = decisionObject.value(QStringLiteral("decided_at")).toString();
    item.decision.note = decisionObject.value(QStringLiteral("note")).toString();

    item.createdAt = payload.value(QStringLiteral("created_at")).toString();
    item.updatedAt = payload.value(QStringLiteral("updated_at")).toString();
    return item;
}

RuntimePermissionListResult readRuntimePermissionListResult(const QJsonObject &payload)
{
    RuntimePermissionListResult result;
    result.taskId = payload.value(QStringLiteral("task_id")).toString();
    result.total = payload.value(QStringLiteral("total")).toInt();

    const QJsonArray permissionsArray = payload.value(QStringLiteral("permissions")).toArray();
    result.permissions.reserve(permissionsArray.size());
    for (const QJsonValue &value : permissionsArray) {
        const RuntimePermissionItem item = readRuntimePermissionItem(value.toObject());
        if (!item.request.requestId.isEmpty()) {
            result.permissions.append(item);
        }
    }

    return result;
}

WorkflowStepRunInfo readWorkflowStepRunInfo(const QJsonObject &payload)
{
    WorkflowStepRunInfo step;
    step.stepId = payload.value(QStringLiteral("step_id")).toString();
    step.agent = payload.value(QStringLiteral("agent")).toString();
    step.action = payload.value(QStringLiteral("action")).toString();
    step.status = payload.value(QStringLiteral("status")).toString();
    step.message = payload.value(QStringLiteral("message")).toString();
    step.requiresConfirmation = payload.value(QStringLiteral("requires_confirmation")).toBool();
    step.riskLevel = payload.value(QStringLiteral("risk_level")).toString(QStringLiteral("low"));
    step.output = payload.value(QStringLiteral("output")).toObject();
    return step;
}

TaskStepListResult readTaskStepListResult(const QJsonObject &payload)
{
    TaskStepListResult result;
    result.taskId = payload.value(QStringLiteral("task_id")).toString();
    result.total = payload.value(QStringLiteral("total")).toInt();

    const QJsonArray stepsArray = payload.value(QStringLiteral("steps")).toArray();
    result.steps.reserve(stepsArray.size());
    for (const QJsonValue &value : stepsArray) {
        const WorkflowStepRunInfo step = readWorkflowStepRunInfo(value.toObject());
        if (!step.stepId.isEmpty()) {
            result.steps.append(step);
        }
    }

    return result;
}

WorkflowRuntimeStateInfo readWorkflowRuntimeStateInfo(const QJsonObject &payload)
{
    WorkflowRuntimeStateInfo result;
    result.taskId = payload.value(QStringLiteral("task_id")).toString();
    result.mode = payload.value(QStringLiteral("mode")).toString();
    result.status = payload.value(QStringLiteral("status")).toString();
    result.terminal = payload.value(QStringLiteral("terminal")).toBool();
    result.allowedActions = readStringList(payload.value(QStringLiteral("allowed_actions")).toArray());
    result.allowedNextStatuses = readStringList(payload.value(QStringLiteral("allowed_next_statuses")).toArray());
    result.message = payload.value(QStringLiteral("message")).toString();
    return result;
}

RuntimeExecutionLimitsInfo readRuntimeExecutionLimitsInfo(const QJsonObject &payload)
{
    RuntimeExecutionLimitsInfo limits;
    limits.maxSteps = payload.value(QStringLiteral("max_steps")).toInt();
    limits.maxToolCalls = payload.value(QStringLiteral("max_tool_calls")).toInt();
    limits.maxRetriesPerTool = payload.value(QStringLiteral("max_retries_per_tool")).toInt();
    limits.toolTimeoutMs = payload.value(QStringLiteral("tool_timeout_ms")).toInt();
    limits.taskTimeoutMs = payload.value(QStringLiteral("task_timeout_ms")).toInt();

    const QJsonValue tokenBudgetValue = payload.value(QStringLiteral("token_budget"));
    limits.tokenBudget = tokenBudgetValue.isDouble() ? tokenBudgetValue.toInt() : -1;
    return limits;
}

RuntimeExecutionMetricsInfo readRuntimeExecutionMetricsInfo(const QJsonObject &payload)
{
    RuntimeExecutionMetricsInfo metrics;
    metrics.startedAt = payload.value(QStringLiteral("started_at")).toString();
    metrics.finishedAt = payload.value(QStringLiteral("finished_at")).toString();
    metrics.durationMs = payload.value(QStringLiteral("duration_ms")).toInt();
    metrics.stepTotal = payload.value(QStringLiteral("step_total")).toInt();
    metrics.stepCompleted = payload.value(QStringLiteral("step_completed")).toInt();
    metrics.stepFailed = payload.value(QStringLiteral("step_failed")).toInt();
    metrics.toolCallTotal = payload.value(QStringLiteral("tool_call_total")).toInt();
    metrics.toolCallSimulated = payload.value(QStringLiteral("tool_call_simulated")).toInt();
    metrics.toolCallFailed = payload.value(QStringLiteral("tool_call_failed")).toInt();
    metrics.retryTotal = payload.value(QStringLiteral("retry_total")).toInt();
    metrics.permissionRequestTotal = payload.value(QStringLiteral("permission_request_total")).toInt();
    metrics.validationErrorTotal = payload.value(QStringLiteral("validation_error_total")).toInt();
    metrics.estimatedInputTokens = payload.value(QStringLiteral("estimated_input_tokens")).toInt();
    metrics.estimatedOutputTokens = payload.value(QStringLiteral("estimated_output_tokens")).toInt();
    metrics.estimatedCostCny = payload.value(QStringLiteral("estimated_cost_cny")).toDouble();
    metrics.budgetExceeded = payload.value(QStringLiteral("budget_exceeded")).toBool();
    return metrics;
}

WorkflowRuntimeMetricsResult readWorkflowRuntimeMetricsResult(const QJsonObject &payload)
{
    WorkflowRuntimeMetricsResult result;
    result.taskId = payload.value(QStringLiteral("task_id")).toString();

    // 后端把 limits 和 metrics 分开返回：limits 是预算边界，metrics 是本次执行事实。
    // Qt 端保持同样分层，避免把“允许上限”和“实际发生”混在一个展示字段里。
    result.limits = readRuntimeExecutionLimitsInfo(payload.value(QStringLiteral("limits")).toObject());
    result.metrics = readRuntimeExecutionMetricsInfo(payload.value(QStringLiteral("metrics")).toObject());
    return result;
}

WorkflowModelRouteAuditInfo readWorkflowModelRouteAuditInfo(const QJsonObject &payload)
{
    // 此处故意只读取后端白名单字段。即使未来响应中意外出现其它配置字段，Qt 也不会把它
    // 展示到历史页，避免审计界面成为认证或材料信息的旁路。
    WorkflowModelRouteAuditInfo info;
    info.stage = payload.value(QStringLiteral("stage")).toString();
    info.routeId = payload.value(QStringLiteral("route_id")).toString();
    info.profileId = payload.value(QStringLiteral("profile_id")).toString();
    info.mode = payload.value(QStringLiteral("mode")).toString();
    info.provider = payload.value(QStringLiteral("provider")).toString();
    info.label = payload.value(QStringLiteral("label")).toString();
    info.model = payload.value(QStringLiteral("model")).toString();
    info.thinking = payload.value(QStringLiteral("thinking")).toString();
    info.compatibility = payload.value(QStringLiteral("compatibility")).toString();
    info.note = payload.value(QStringLiteral("note")).toString();
    return info;
}

WorkflowModelRouteAuditResult readWorkflowModelRouteAuditResult(const QJsonObject &payload)
{
    WorkflowModelRouteAuditResult result;
    result.taskId = payload.value(QStringLiteral("task_id")).toString();

    const QJsonArray routesArray = payload.value(QStringLiteral("model_routes")).toArray();
    result.modelRoutes.reserve(routesArray.size());
    for (const QJsonValue &value : routesArray) {
        const WorkflowModelRouteAuditInfo info = readWorkflowModelRouteAuditInfo(value.toObject());
        if (!info.routeId.isEmpty() && !info.model.isEmpty()) {
            result.modelRoutes.append(info);
        }
    }
    return result;
}

WorkflowTaskEvaluationResult readWorkflowTaskEvaluationResult(const QJsonObject &payload)
{
    WorkflowTaskEvaluationResult result;
    result.taskId = payload.value(QStringLiteral("task_id")).toString();
    result.mode = payload.value(QStringLiteral("mode")).toString();
    result.status = payload.value(QStringLiteral("status")).toString();
    result.outcome = payload.value(QStringLiteral("outcome")).toString();
    result.summary = payload.value(QStringLiteral("summary")).toString();
    result.stepSuccessRate = payload.value(QStringLiteral("step_success_rate")).toDouble();
    result.toolSuccessRate = payload.value(QStringLiteral("tool_success_rate")).toDouble();
    result.efficiencyScore = payload.value(QStringLiteral("efficiency_score")).toDouble();
    result.overallScore = payload.value(QStringLiteral("overall_score")).toDouble();
    result.durationMs = payload.value(QStringLiteral("duration_ms")).toInt();
    result.retryTotal = payload.value(QStringLiteral("retry_total")).toInt();
    result.failedToolCalls = payload.value(QStringLiteral("failed_tool_calls")).toInt();
    result.blockedToolCalls = payload.value(QStringLiteral("blocked_tool_calls")).toInt();
    result.pendingPermissions = payload.value(QStringLiteral("pending_permissions")).toInt();
    result.deniedPermissions = payload.value(QStringLiteral("denied_permissions")).toInt();
    result.warnings = readStringList(payload.value(QStringLiteral("warnings")).toArray());
    result.recommendations = readStringList(payload.value(QStringLiteral("recommendations")).toArray());
    return result;
}

WorkflowNodeContractInfo readWorkflowNodeContractInfo(const QJsonObject &payload)
{
    WorkflowNodeContractInfo contract;
    contract.agentId = payload.value(QStringLiteral("agent_id")).toString();
    contract.action = payload.value(QStringLiteral("action")).toString();
    contract.toolName = payload.value(QStringLiteral("tool_name")).toString();
    contract.nodeType = payload.value(QStringLiteral("node_type")).toString();
    contract.inputSchema = payload.value(QStringLiteral("input_schema")).toObject();
    contract.outputSchema = payload.value(QStringLiteral("output_schema")).toObject();
    contract.stateWrites = readStringList(payload.value(QStringLiteral("state_writes")).toArray());
    contract.requiredPermissions = readStringList(payload.value(QStringLiteral("required_permissions")).toArray());
    contract.failureCodes = readStringList(payload.value(QStringLiteral("failure_codes")).toArray());
    contract.evaluationSignals = readStringList(payload.value(QStringLiteral("evaluation_signals")).toArray());
    return contract;
}

WorkflowNodeContractListResult readWorkflowNodeContractListResult(const QJsonObject &payload)
{
    WorkflowNodeContractListResult result;
    result.total = payload.value(QStringLiteral("total")).toInt();

    const QJsonArray contractsArray = payload.value(QStringLiteral("contracts")).toArray();
    result.contracts.reserve(contractsArray.size());
    for (const QJsonValue &value : contractsArray) {
        const WorkflowNodeContractInfo contract = readWorkflowNodeContractInfo(value.toObject());
        if (!contract.agentId.isEmpty() && !contract.action.isEmpty() && !contract.toolName.isEmpty()) {
            result.contracts.append(contract);
        }
    }

    return result;
}

WorkflowCommandPolicyCheckResult readWorkflowCommandPolicyCheckResult(const QJsonObject &payload)
{
    WorkflowCommandPolicyCheckResult result;
    result.command = payload.value(QStringLiteral("command")).toString();
    result.normalizedCommand = payload.value(QStringLiteral("normalized_command")).toString();
    result.riskLevel = payload.value(QStringLiteral("risk_level")).toString(QStringLiteral("none"));
    result.allowed = payload.value(QStringLiteral("allowed")).toBool(true);
    result.requiresConfirmation = payload.value(QStringLiteral("requires_confirmation")).toBool();
    result.auditRequired = payload.value(QStringLiteral("audit_required")).toBool(true);
    result.concurrencySafe = payload.value(QStringLiteral("concurrency_safe")).toBool();
    result.defaultTimeoutMs = payload.value(QStringLiteral("default_timeout_ms")).toInt();
    result.maxOutputChars = payload.value(QStringLiteral("max_output_chars")).toInt();
    result.detectedCommands = readStringList(payload.value(QStringLiteral("detected_commands")).toArray());
    result.categories = readStringList(payload.value(QStringLiteral("categories")).toArray());
    result.ruleIds = readStringList(payload.value(QStringLiteral("rule_ids")).toArray());
    result.reasons = readStringList(payload.value(QStringLiteral("reasons")).toArray());
    result.destructiveWarnings = readStringList(payload.value(QStringLiteral("destructive_warnings")).toArray());
    result.saferAlternatives = readStringList(payload.value(QStringLiteral("safer_alternatives")).toArray());
    result.warnings = readStringList(payload.value(QStringLiteral("warnings")).toArray());
    result.suggestedTool = payload.value(QStringLiteral("suggested_tool")).toString();
    result.effectivePermissionPolicy = payload.value(QStringLiteral("effective_permission_policy"))
                                           .toString(QStringLiteral("smart_confirm"));
    result.effectiveAction = payload.value(QStringLiteral("effective_action")).toString(QStringLiteral("confirm"));
    result.effectiveReason = payload.value(QStringLiteral("effective_reason")).toString();
    result.executionScope = payload.value(QStringLiteral("execution_scope")).toString(QStringLiteral("none"));
    result.executionRoute = payload.value(QStringLiteral("execution_route")).toString();
    result.cwdPolicy = payload.value(QStringLiteral("cwd_policy")).toString();
    result.sandboxHint = payload.value(QStringLiteral("sandbox_hint")).toString();
    result.auditFields = readStringList(payload.value(QStringLiteral("audit_fields")).toArray());
    result.executionNotes = readStringList(payload.value(QStringLiteral("execution_notes")).toArray());
    result.runtimeReady = payload.value(QStringLiteral("runtime_ready")).toBool();
    result.permissionRequired = payload.value(QStringLiteral("permission_required")).toBool();
    result.runtimeRequestStatus = payload.value(QStringLiteral("runtime_request_status")).toString(QStringLiteral("blocked"));
    result.approvalPrompt = payload.value(QStringLiteral("approval_prompt")).toString();
    result.blockReasonCode = payload.value(QStringLiteral("block_reason_code")).toString();
    result.auditRecordPreview = payload.value(QStringLiteral("audit_record_preview")).toObject();
    return result;
}

RuntimePreferencesResult readRuntimePreferencesResult(const QJsonObject &payload)
{
    RuntimePreferencesResult result;
    result.permissionPolicy = payload.value(QStringLiteral("permission_policy")).toString(QStringLiteral("smart_confirm"));
    result.personality = payload.value(QStringLiteral("personality")).toString(QStringLiteral("professional"));
    result.memoryEnabled = payload.value(QStringLiteral("memory_enabled")).toBool(false);
    result.updatedAt = payload.value(QStringLiteral("updated_at")).toString();
    result.notes = payload.value(QStringLiteral("notes")).toString();
    return result;
}

LongTermMemoryInfo readLongTermMemoryInfo(const QJsonObject &payload)
{
    LongTermMemoryInfo result;
    result.memoryId = payload.value(QStringLiteral("memory_id")).toString();
    result.kind = payload.value(QStringLiteral("kind")).toString();
    result.scope = payload.value(QStringLiteral("scope")).toString();
    result.title = payload.value(QStringLiteral("title")).toString();
    result.summary = payload.value(QStringLiteral("summary")).toString();
    for (const QJsonValue &value : payload.value(QStringLiteral("tags")).toArray()) {
        const QString tag = value.toString().trimmed();
        if (!tag.isEmpty()) {
            result.tags.append(tag);
        }
    }
    result.sourceTaskId = payload.value(QStringLiteral("source_task_id")).toString();
    result.userConfirmed = payload.value(QStringLiteral("user_confirmed")).toBool(false);
    result.enabled = payload.value(QStringLiteral("enabled")).toBool(true);
    result.createdAt = payload.value(QStringLiteral("created_at")).toString();
    result.updatedAt = payload.value(QStringLiteral("updated_at")).toString();
    result.lastUsedAt = payload.value(QStringLiteral("last_used_at")).toString();
    return result;
}

TaskMemoryProposalInfo readTaskMemoryProposalInfo(const QJsonObject &payload)
{
    TaskMemoryProposalInfo result;
    result.proposalId = payload.value(QStringLiteral("proposal_id")).toString();
    result.taskId = payload.value(QStringLiteral("task_id")).toString();
    result.kind = payload.value(QStringLiteral("kind")).toString();
    result.title = payload.value(QStringLiteral("title")).toString();
    result.summary = payload.value(QStringLiteral("summary")).toString();
    result.tags = readStringList(payload.value(QStringLiteral("tags")).toArray());
    result.suggestedScope = payload.value(QStringLiteral("suggested_scope"))
                                .toString(result.suggestedScope);
    result.reason = payload.value(QStringLiteral("reason")).toString();
    result.requiresUserConfirmation = payload.value(QStringLiteral("requires_user_confirmation"))
                                          .toBool(true);
    return result;
}

WorkflowArtifactInfo readWorkflowArtifactInfo(const QJsonObject &payload)
{
    WorkflowArtifactInfo artifact;
    artifact.artifactId = payload.value(QStringLiteral("artifact_id")).toString();
    artifact.taskId = payload.value(QStringLiteral("task_id")).toString();
    artifact.stepId = payload.value(QStringLiteral("step_id")).toString();
    artifact.agentId = payload.value(QStringLiteral("agent_id")).toString();
    artifact.kind = payload.value(QStringLiteral("kind")).toString(QStringLiteral("other"));
    artifact.name = payload.value(QStringLiteral("name")).toString();
    artifact.summary = payload.value(QStringLiteral("summary")).toString();
    artifact.uri = payload.value(QStringLiteral("uri")).toString();
    artifact.mimeType = payload.value(QStringLiteral("mime_type")).toString(QStringLiteral("text/plain"));
    artifact.metadata = payload.value(QStringLiteral("metadata")).toObject();
    artifact.createdAt = payload.value(QStringLiteral("created_at")).toString();
    return artifact;
}

PdfProcessingTaskResult readPdfProcessingTaskResult(const QJsonObject &payload)
{
    PdfProcessingTaskResult result;
    result.taskId = payload.value(QStringLiteral("task_id")).toString();
    result.status = payload.value(QStringLiteral("status")).toString();
    result.operation = payload.value(QStringLiteral("operation")).toString();
    result.summary = payload.value(QStringLiteral("summary")).toString();
    result.message = payload.value(QStringLiteral("message")).toString();
    const QJsonObject artifactPayload = payload.value(QStringLiteral("artifact")).toObject();
    if (!artifactPayload.isEmpty()) {
        result.artifact = readWorkflowArtifactInfo(artifactPayload);
        result.hasArtifact = !result.artifact.artifactId.isEmpty();
    }
    result.verification = payload.value(QStringLiteral("verification")).toObject();
    return result;
}

WorkflowArtifactListResult readWorkflowArtifactListResult(const QJsonObject &payload)
{
    WorkflowArtifactListResult result;
    result.taskId = payload.value(QStringLiteral("task_id")).toString();
    result.total = payload.value(QStringLiteral("total")).toInt();

    const QJsonArray artifactsArray = payload.value(QStringLiteral("artifacts")).toArray();
    result.artifacts.reserve(artifactsArray.size());
    for (const QJsonValue &value : artifactsArray) {
        const WorkflowArtifactInfo artifact = readWorkflowArtifactInfo(value.toObject());
        if (!artifact.artifactId.isEmpty()) {
            result.artifacts.append(artifact);
        }
    }

    return result;
}

WorkflowDeliveryCardInfo readWorkflowDeliveryCardInfo(const QJsonObject &payload)
{
    WorkflowDeliveryCardInfo result;
    result.schemaVersion = payload.value(QStringLiteral("schema_version")).toString();
    result.deliveryId = payload.value(QStringLiteral("delivery_id")).toString();
    result.taskId = payload.value(QStringLiteral("task_id")).toString();
    result.mode = payload.value(QStringLiteral("mode")).toString();
    result.status = payload.value(QStringLiteral("status")).toString();
    result.terminal = payload.value(QStringLiteral("terminal")).toBool();
    result.headline = payload.value(QStringLiteral("headline")).toString();
    result.summaryMarkdown = payload.value(QStringLiteral("summary_markdown")).toString();
    result.warnings = readStringList(payload.value(QStringLiteral("warnings")).toArray());
    result.nextActions = readStringList(payload.value(QStringLiteral("next_actions")).toArray());
    result.updatedAt = payload.value(QStringLiteral("updated_at")).toString();

    const QJsonObject tableSummary = payload.value(QStringLiteral("table_summary")).toObject();
    if (!tableSummary.isEmpty()) {
        result.hasTableSummary = true;
        result.tableSummary.tableCount = tableSummary.value(QStringLiteral("table_count")).toInt();
        result.tableSummary.chartCount = tableSummary.value(QStringLiteral("chart_count")).toInt();
        result.tableSummary.metricCount = tableSummary.value(QStringLiteral("metric_count")).toInt();
        result.tableSummary.description = tableSummary.value(QStringLiteral("description")).toString();
    }

    const QJsonArray facts = payload.value(QStringLiteral("facts")).toArray();
    result.facts.reserve(facts.size());
    for (const QJsonValue &value : facts) {
        const QJsonObject factPayload = value.toObject();
        WorkflowDeliveryFactInfo fact;
        fact.label = factPayload.value(QStringLiteral("label")).toString();
        fact.value = factPayload.value(QStringLiteral("value")).toString();
        if (!fact.label.isEmpty() || !fact.value.isEmpty()) {
            result.facts.append(fact);
        }
    }

    const QJsonArray artifacts = payload.value(QStringLiteral("artifacts")).toArray();
    result.artifacts.reserve(artifacts.size());
    for (const QJsonValue &value : artifacts) {
        const QJsonObject artifactPayload = value.toObject();
        WorkflowDeliveryArtifactInfo artifact;
        artifact.artifactId = artifactPayload.value(QStringLiteral("artifact_id")).toString();
        artifact.name = artifactPayload.value(QStringLiteral("name")).toString();
        artifact.kind = artifactPayload.value(QStringLiteral("kind")).toString();
        artifact.summary = artifactPayload.value(QStringLiteral("summary")).toString();
        artifact.uri = artifactPayload.value(QStringLiteral("uri")).toString();
        artifact.mimeType = artifactPayload.value(QStringLiteral("mime_type")).toString();
        artifact.openable = artifactPayload.value(QStringLiteral("openable")).toBool();
        artifact.previewable = artifactPayload.value(QStringLiteral("previewable")).toBool();
        artifact.sourceTaskId = artifactPayload.value(QStringLiteral("source_task_id")).toString();
        if (!artifact.artifactId.isEmpty() || !artifact.name.isEmpty()) {
            result.artifacts.append(artifact);
        }
    }

    return result;
}

WorkflowArtifactPreviewResult readWorkflowArtifactPreviewResult(const QJsonObject &payload)
{
    WorkflowArtifactPreviewResult result;
    result.taskId = payload.value(QStringLiteral("task_id")).toString();
    result.artifactId = payload.value(QStringLiteral("artifact_id")).toString();
    result.available = payload.value(QStringLiteral("available")).toBool();
    result.reason = payload.value(QStringLiteral("reason")).toString();
    result.kind = payload.value(QStringLiteral("kind")).toString(QStringLiteral("other"));
    result.name = payload.value(QStringLiteral("name")).toString();
    result.uri = payload.value(QStringLiteral("uri")).toString();
    result.mimeType = payload.value(QStringLiteral("mime_type")).toString(QStringLiteral("text/plain"));
    result.source = payload.value(QStringLiteral("source")).toString(QStringLiteral("unavailable"));
    result.text = payload.value(QStringLiteral("text")).toString();
    result.encoding = payload.value(QStringLiteral("encoding")).toString(QStringLiteral("utf-8"));
    result.bytesRead = payload.value(QStringLiteral("bytes_read")).toInt();
    result.truncated = payload.value(QStringLiteral("truncated")).toBool();
    result.metadata = payload.value(QStringLiteral("metadata")).toObject();
    return result;
}

WorkflowToolCallInfo readWorkflowToolCallInfo(const QJsonObject &payload)
{
    WorkflowToolCallInfo call;
    call.callId = payload.value(QStringLiteral("call_id")).toString();
    call.taskId = payload.value(QStringLiteral("task_id")).toString();
    call.stepId = payload.value(QStringLiteral("step_id")).toString();
    call.agentId = payload.value(QStringLiteral("agent_id")).toString();
    call.toolName = payload.value(QStringLiteral("tool_name")).toString();
    call.status = payload.value(QStringLiteral("status")).toString(QStringLiteral("simulated"));
    call.riskLevel = payload.value(QStringLiteral("risk_level")).toString(QStringLiteral("low"));
    call.permissionRequired = payload.value(QStringLiteral("permission_required")).toBool();
    call.attempt = payload.value(QStringLiteral("attempt")).toInt(1);
    call.maxAttempts = payload.value(QStringLiteral("max_attempts")).toInt(3);
    call.timeoutMs = payload.value(QStringLiteral("timeout_ms")).toInt();
    call.durationMs = payload.value(QStringLiteral("duration_ms")).toInt();
    call.failureCount = payload.value(QStringLiteral("failure_count")).toInt();
    call.request = payload.value(QStringLiteral("request")).toObject();
    call.result = payload.value(QStringLiteral("result")).toObject();
    call.error = payload.value(QStringLiteral("error")).toString();
    call.startedAt = payload.value(QStringLiteral("started_at")).toString();
    call.finishedAt = payload.value(QStringLiteral("finished_at")).toString();
    return call;
}

WorkflowToolCallListResult readWorkflowToolCallListResult(const QJsonObject &payload)
{
    WorkflowToolCallListResult result;
    result.taskId = payload.value(QStringLiteral("task_id")).toString();
    result.total = payload.value(QStringLiteral("total")).toInt();

    const QJsonArray toolCallsArray = payload.value(QStringLiteral("tool_calls")).toArray();
    result.toolCalls.reserve(toolCallsArray.size());
    for (const QJsonValue &value : toolCallsArray) {
        const WorkflowToolCallInfo call = readWorkflowToolCallInfo(value.toObject());
        if (!call.callId.isEmpty()) {
            result.toolCalls.append(call);
        }
    }

    return result;
}

WorkflowExecutionResult readWorkflowExecutionResult(const QJsonObject &payload)
{
    WorkflowExecutionResult result;
    result.sourceTaskId = payload.value(QStringLiteral("source_task_id")).toString();
    result.runtimeTaskId = payload.value(QStringLiteral("runtime_task_id")).toString();
    result.accepted = payload.value(QStringLiteral("accepted")).toBool();
    result.status = payload.value(QStringLiteral("status")).toString();
    result.message = payload.value(QStringLiteral("message")).toString();
    result.workflowRun = payload.value(QStringLiteral("workflow_run")).toObject();
    return result;
}

ModelProviderInfo readModelProviderInfo(const QJsonObject &payload)
{
    // 后端使用 snake_case；Qt 端结构体使用更贴近 C++ 习惯的 camelCase。
    // 所有协议字段转换集中在这里，模型页就不用直接依赖 JSON 字段名。
    ModelProviderInfo provider;
    provider.provider = payload.value(QStringLiteral("provider")).toString();
    provider.label = payload.value(QStringLiteral("label")).toString();
    provider.transport = payload.value(QStringLiteral("transport")).toString();
    provider.defaultBaseUrl = payload.value(QStringLiteral("default_base_url")).toString();
    provider.defaultModel = payload.value(QStringLiteral("default_model")).toString();
    provider.supportsThinking = payload.value(QStringLiteral("supports_thinking")).toBool();
    provider.supportsJsonOutput = payload.value(QStringLiteral("supports_json_output")).toBool(true);
    provider.supportsToolCalls = payload.value(QStringLiteral("supports_tool_calls")).toBool(true);
    provider.apiKeyConfigured = payload.value(QStringLiteral("api_key_configured")).toBool();
    provider.notes = payload.value(QStringLiteral("notes")).toString();
    return provider;
}

ModelProviderStatus readModelProviderStatus(const QJsonObject &payload)
{
    ModelProviderStatus status;
    status.provider = payload.value(QStringLiteral("provider")).toString();
    status.label = payload.value(QStringLiteral("label")).toString();
    status.transport = payload.value(QStringLiteral("transport")).toString();
    status.baseUrl = payload.value(QStringLiteral("base_url")).toString();
    status.model = payload.value(QStringLiteral("model")).toString();
    status.thinking = payload.value(QStringLiteral("thinking")).toString(QStringLiteral("disabled"));
    status.apiKeySource = payload.value(QStringLiteral("api_key_source")).toString();
    status.configurationSource = payload.value(QStringLiteral("configuration_source")).toString();
    status.secureStorage = payload.value(QStringLiteral("secure_storage")).toString();
    status.apiKeyConfigured = payload.value(QStringLiteral("api_key_configured")).toBool();
    status.secureStorageAvailable = payload.value(QStringLiteral("secure_storage_available")).toBool();
    status.supportsThinking = payload.value(QStringLiteral("supports_thinking")).toBool();
    status.notes = payload.value(QStringLiteral("notes")).toString();
    status.configurationError = payload.value(QStringLiteral("configuration_error")).toString();
    return status;
}

ModelProviderListResult readModelProviderListResult(const QJsonObject &payload)
{
    ModelProviderListResult result;
    result.current = readModelProviderStatus(payload.value(QStringLiteral("current")).toObject());

    const QJsonArray providersArray = payload.value(QStringLiteral("providers")).toArray();
    result.providers.reserve(providersArray.size());
    for (const QJsonValue &value : providersArray) {
        const ModelProviderInfo provider = readModelProviderInfo(value.toObject());
        if (!provider.provider.isEmpty()) {
            result.providers.append(provider);
        }
    }

    return result;
}

ModelRouteInfo readModelRouteInfo(const QJsonObject &payload)
{
    // 作用域路由与 Provider 配置分开解析：前者不应有 API Key，也不需要 Qt 从 JSON 推断
    // “当前实际模型”。缺少 resolved 只代表当前路由不可用或处于预留状态。
    ModelRouteInfo route;
    route.routeId = payload.value(QStringLiteral("route_id")).toString();
    route.label = payload.value(QStringLiteral("label")).toString();
    route.description = payload.value(QStringLiteral("description")).toString();
    const QJsonArray capabilities = payload.value(QStringLiteral("required_capabilities")).toArray();
    for (const QJsonValue &value : capabilities) {
        const QString capability = value.toString().trimmed();
        if (!capability.isEmpty()) {
            route.requiredCapabilities.append(capability);
        }
    }

    const QJsonObject settings = payload.value(QStringLiteral("settings")).toObject();
    route.mode = settings.value(QStringLiteral("mode")).toString(QStringLiteral("inherit_global"));
    route.provider = settings.value(QStringLiteral("provider")).toString();
    route.baseUrl = settings.value(QStringLiteral("base_url")).toString();
    route.model = settings.value(QStringLiteral("model")).toString();
    route.thinking = settings.value(QStringLiteral("thinking")).toString(QStringLiteral("disabled"));
    route.updatedAt = settings.value(QStringLiteral("updated_at")).toString();
    route.availability = payload.value(QStringLiteral("availability")).toString();
    route.availabilityMessage = payload.value(QStringLiteral("availability_message")).toString();

    const QJsonObject resolved = payload.value(QStringLiteral("resolved")).toObject();
    route.hasResolved = !resolved.isEmpty();
    route.resolvedProfileId = resolved.value(QStringLiteral("profile_id")).toString();
    route.resolvedProvider = resolved.value(QStringLiteral("provider")).toString();
    route.resolvedLabel = resolved.value(QStringLiteral("label")).toString();
    route.resolvedModel = resolved.value(QStringLiteral("model")).toString();
    route.resolvedThinking = resolved.value(QStringLiteral("thinking")).toString();
    route.resolvedCompatibility = resolved.value(QStringLiteral("compatibility")).toString();
    route.resolvedNote = resolved.value(QStringLiteral("note")).toString();
    return route;
}

ModelRouteListResult readModelRouteListResult(const QJsonObject &payload)
{
    ModelRouteListResult result;
    const QJsonArray routesArray = payload.value(QStringLiteral("routes")).toArray();
    result.routes.reserve(routesArray.size());
    for (const QJsonValue &value : routesArray) {
        const ModelRouteInfo route = readModelRouteInfo(value.toObject());
        if (!route.routeId.isEmpty()) {
            result.routes.append(route);
        }
    }
    return result;
}

ModelConnectionTestResult readModelConnectionTestResult(const QJsonObject &payload)
{
    ModelConnectionTestResult result;
    result.ok = payload.value(QStringLiteral("ok")).toBool();
    result.provider = payload.value(QStringLiteral("provider")).toString();
    result.label = payload.value(QStringLiteral("label")).toString();
    result.transport = payload.value(QStringLiteral("transport")).toString();
    result.baseUrl = payload.value(QStringLiteral("base_url")).toString();
    result.model = payload.value(QStringLiteral("model")).toString();
    result.apiKeySource = payload.value(QStringLiteral("api_key_source")).toString();
    result.elapsedMs = payload.value(QStringLiteral("elapsed_ms")).toInt();
    result.message = payload.value(QStringLiteral("message")).toString();
    result.responsePreview = payload.value(QStringLiteral("response_preview")).toString();
    return result;
}

TaskControlResult readTaskControlResult(const QJsonObject &payload)
{
    TaskControlResult result;
    result.taskId = payload.value(QStringLiteral("task_id")).toString();
    result.action = payload.value(QStringLiteral("action")).toString();
    result.accepted = payload.value(QStringLiteral("accepted")).toBool();
    result.status = payload.value(QStringLiteral("status")).toString();
    result.message = payload.value(QStringLiteral("message")).toString();
    result.newTaskId = payload.value(QStringLiteral("new_task_id")).toString();
    return result;
}

} // namespace

BackendClient::BackendClient(QObject *parent)
    : QObject(parent)
    , baseUrl_(QStringLiteral("http://127.0.0.1:8765"))
{
    // BackendClient 统一拥有 WebSocket 生命周期。
    // MainWindow 只接收信号，不直接操作 socket，后面替换任务日志来源更轻。
    connect(&taskSocket_, &QWebSocket::textMessageReceived, this, &BackendClient::handleTaskLogMessage);
    connect(&taskSocket_, &QWebSocket::disconnected, this, [this]() {
        if (activeLogTaskId_.isEmpty()) {
            return;
        }

        const QString taskId = activeLogTaskId_;
        activeLogTaskId_.clear();
        if (!taskSocketHadError_) {
            emit taskLogFinished(taskId);
        }
    });
    connect(&taskSocket_, &QWebSocket::errorOccurred, this, [this](QAbstractSocket::SocketError) {
        taskSocketHadError_ = true;
        emit taskLogFailed(taskSocket_.errorString());
    });
}

void BackendClient::refresh()
{
    requestHealth();
}

void BackendClient::sendChatMessage(const QString &message,
                                    const QString &agentId,
                                    const QJsonArray &materials,
                                    const QString &projectScope,
                                    const QString &conversationId,
                                    const QJsonArray &agentHints)
{
    // 附件只以材料绑定形式发送，和后端 ChatRequest 保持一致；绝不把绝对路径或内容正文
    // 当作聊天上下文传输。模型是否最终看得到材料，仍由专业 Agent 的 Tool 控制。
    QJsonObject payload;
    payload.insert(QStringLiteral("message"), message);
    payload.insert(QStringLiteral("agent_id"), agentId.isEmpty() ? QStringLiteral("commander_agent") : agentId);
    payload.insert(QStringLiteral("project_scope"),
                   projectScope.trimmed().isEmpty() ? QStringLiteral("global") : projectScope.trimmed());
    if (!conversationId.trimmed().isEmpty()) {
        payload.insert(QStringLiteral("conversation_id"), conversationId.trimmed());
    }
    if (!materials.isEmpty()) {
        payload.insert(QStringLiteral("materials"), materials);
    }
    if (!agentHints.isEmpty()) {
        // `@` 标签只发有限的已知 Agent ID；后端还会从文本二次规范化，不能借此绕过
        // action admission、材料范围或任何权限确认。
        payload.insert(QStringLiteral("agent_hints"), agentHints);
    }

    QNetworkReply *reply = networkManager_.post(
        // 真实模型请求可能需要几十秒；健康检查等短请求仍保持 3 秒超时。
        createRequest(QStringLiteral("/api/chat"), 120000),
        QJsonDocument(payload).toJson(QJsonDocument::Compact));

    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        reply->deleteLater();

        if (reply->error() != QNetworkReply::NoError) {
            emit chatFailed(replyErrorMessage(reply));
            return;
        }

        const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
        const QJsonObject payload = document.object();
        ChatResult result;
        result.taskId = payload.value(QStringLiteral("task_id")).toString();
        result.agentId = payload.value(QStringLiteral("agent_id")).toString();
        result.conversationId = payload.value(QStringLiteral("conversation_id")).toString();
        result.mode = payload.value(QStringLiteral("mode")).toString(QStringLiteral("mock"));
        result.model = payload.value(QStringLiteral("model")).toString();
        result.reply = payload.value(QStringLiteral("reply")).toString();
        const QJsonObject workflowPlan = payload.value(QStringLiteral("workflow_plan")).toObject();
        result.planSummary = readWorkflowPlanSummaryInfo(workflowPlan);
        result.steps = readWorkflowSteps(workflowPlan);

        if (result.taskId.isEmpty() || result.reply.isEmpty()) {
            emit chatFailed(QStringLiteral("后端聊天响应缺少 task_id 或 reply。"));
            return;
        }

        emit chatCompleted(result);
    });
}

void BackendClient::requestConversationContext(const QString &conversationId)
{
    const QString normalizedId = conversationId.trimmed();
    if (normalizedId.isEmpty()) {
        emit conversationContextFailed(QStringLiteral("会话标识为空，无法恢复记录。"));
        return;
    }

    const QString encodedId = QString::fromLatin1(QUrl::toPercentEncoding(normalizedId));
    QNetworkReply *reply = networkManager_.get(
        createRequest(QStringLiteral("/api/chat/conversations/%1").arg(encodedId), 10000));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        if (reply->error() != QNetworkReply::NoError) {
            const QString message = replyErrorMessage(reply);
            reply->deleteLater();
            emit conversationContextFailed(message);
            return;
        }

        const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
        reply->deleteLater();
        if (!document.isObject()) {
            emit conversationContextFailed(QStringLiteral("后端会话恢复响应格式无效。"));
            return;
        }
        const ConversationContextInfo context = readConversationContextInfo(document.object());
        if (context.conversationId.isEmpty()) {
            emit conversationContextFailed(QStringLiteral("后端会话恢复响应缺少会话标识。"));
            return;
        }
        emit conversationContextReceived(context);
    });
}

void BackendClient::requestConversationSessions(const QString &projectScope, int limit)
{
    const QString normalizedScope = projectScope.trimmed().isEmpty() ? QStringLiteral("global") : projectScope.trimmed();
    const int boundedLimit = qBound(1, limit, 80);
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/chat/conversations"));
    QUrlQuery query;
    // `createRequest(QString)` 会把整个参数作为 URL path 设置。若把 `?project_scope=...`
    // 混进 path，Qt 会把问号编码为路径字符，FastAPI 收到的就不再是会话列表路由而是 404。
    // 查询参数必须由 QUrlQuery 单独构造，避免 scope 中的冒号、空格或中文再次破坏 URL。
    query.addQueryItem(QStringLiteral("project_scope"), normalizedScope);
    query.addQueryItem(QStringLiteral("limit"), QString::number(boundedLimit));
    url.setQuery(query);
    QNetworkReply *reply = networkManager_.get(createRequest(url, 10000));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        if (reply->error() != QNetworkReply::NoError) {
            const QString message = replyErrorMessage(reply);
            reply->deleteLater();
            emit conversationSessionsFailed(message);
            return;
        }
        const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
        reply->deleteLater();
        if (!document.isObject()) {
            emit conversationSessionsFailed(QStringLiteral("后端会话列表响应格式无效。"));
            return;
        }
        emit conversationSessionsReceived(readConversationSessionListResult(document.object()));
    });
}

void BackendClient::requestConversationTranscript(
    const QString &conversationId,
    const QString &projectScope,
    int offset,
    int limit)
{
    const QString normalizedId = conversationId.trimmed();
    if (normalizedId.isEmpty()) {
        emit conversationTranscriptFailed(conversationId, QStringLiteral("会话标识为空，无法读取完整记录。"));
        return;
    }
    const QString normalizedScope = projectScope.trimmed().isEmpty() ? QStringLiteral("global") : projectScope.trimmed();
    const int boundedOffset = qMax(0, offset);
    const int boundedLimit = qBound(1, limit, 100);
    QUrl url(baseUrl_);
    // conversation_id 已由服务端限制为受控 ASCII 标识；直接作为 path 片段可避免二次编码。
    url.setPath(QStringLiteral("/api/chat/conversations/%1/messages").arg(normalizedId));
    QUrlQuery query;
    query.addQueryItem(QStringLiteral("project_scope"), normalizedScope);
    query.addQueryItem(QStringLiteral("offset"), QString::number(boundedOffset));
    query.addQueryItem(QStringLiteral("limit"), QString::number(boundedLimit));
    url.setQuery(query);
    QNetworkReply *reply = networkManager_.get(createRequest(url, 10000));
    connect(reply, &QNetworkReply::finished, this, [this, reply, normalizedId]() {
        if (reply->error() != QNetworkReply::NoError) {
            const QString message = replyErrorMessage(reply);
            reply->deleteLater();
            emit conversationTranscriptFailed(normalizedId, message);
            return;
        }
        const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
        reply->deleteLater();
        if (!document.isObject()) {
            emit conversationTranscriptFailed(normalizedId, QStringLiteral("后端完整会话响应格式无效。"));
            return;
        }
        const ConversationTranscriptPageResult result = readConversationTranscriptPageResult(document.object());
        if (result.session.conversationId.isEmpty()) {
            emit conversationTranscriptFailed(normalizedId, QStringLiteral("后端完整会话响应缺少会话标识。"));
            return;
        }
        emit conversationTranscriptReceived(result);
    });
}

void BackendClient::requestTaskHistory(const TaskHistoryQuery &query)
{
    // 历史页会频繁翻页和筛选，所以只发分页 GET，由后端负责聚合和过滤。
    QNetworkReply *reply = networkManager_.get(createRequest(buildTaskHistoryUrl(query)));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleTaskHistoryReply(reply);
    });
}

void BackendClient::requestModelProviders()
{
    // 模型页当前只读展示 provider profile 和当前运行时状态。
    // 这个接口不会触发真实模型调用，也不会返回 API Key。
    QNetworkReply *reply = networkManager_.get(createRequest(buildModelProvidersUrl()));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleModelProvidersReply(reply);
    });
}

void BackendClient::requestModelRoutes()
{
    // 路由列表是低频检查器数据，不发起真实模型调用，也不返回 API Key 或 Base URL 之外的
    // 私有运行时对象。请求完成后由 ModelRouteDialog 按当前选中项更新，不自动改写表单。
    QNetworkReply *reply = networkManager_.get(createRequest(buildModelRoutesUrl(), 6000));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleModelRoutesReply(reply);
    });
}

void BackendClient::saveModelRoute(
    const QString &routeId,
    const QString &mode,
    const QString &provider,
    const QString &baseUrl,
    const QString &model,
    const QString &thinking)
{
    if (routeId.trimmed().isEmpty()) {
        emit modelRouteSaveFailed(QStringLiteral("未选择任务模型作用域。"));
        return;
    }

    QJsonObject payload;
    payload.insert(QStringLiteral("mode"), mode == QStringLiteral("configured")
                                                ? QStringLiteral("configured")
                                                : QStringLiteral("inherit_global"));
    if (payload.value(QStringLiteral("mode")).toString() == QStringLiteral("configured")) {
        payload.insert(QStringLiteral("provider"), provider.trimmed());
        payload.insert(QStringLiteral("base_url"), baseUrl.trimmed());
        payload.insert(QStringLiteral("model"), model.trimmed());
        payload.insert(QStringLiteral("thinking"), thinking == QStringLiteral("enabled")
                                                        ? QStringLiteral("enabled")
                                                        : QStringLiteral("disabled"));
    }

    QNetworkReply *reply = networkManager_.put(
        createRequest(buildModelRouteUrl(routeId.trimmed()), 10000),
        QJsonDocument(payload).toJson(QJsonDocument::Compact));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleModelRouteSaveReply(reply);
    });
}

void BackendClient::saveModelConfig(
    const QString &provider,
    const QString &baseUrl,
    const QString &model,
    const QString &thinking,
    const QString &apiKey,
    bool clearApiKey)
{
    if (provider.trimmed().isEmpty()) {
        emit modelConfigSaveFailed(QStringLiteral("请选择模型供应商。"));
        return;
    }

    QJsonObject payload;
    payload.insert(QStringLiteral("provider"), provider.trimmed());
    payload.insert(QStringLiteral("base_url"), baseUrl.trimmed());
    payload.insert(QStringLiteral("model"), model.trimmed());
    payload.insert(QStringLiteral("thinking"), thinking.isEmpty() ? QStringLiteral("disabled") : thinking);
    payload.insert(QStringLiteral("clear_api_key"), clearApiKey);
    if (!apiKey.trimmed().isEmpty()) {
        // Key 只放在这一次 PUT 请求体里；后端响应和后续状态刷新都只返回脱敏状态。
        payload.insert(QStringLiteral("api_key"), apiKey.trimmed());
    }

    QNetworkReply *reply = networkManager_.put(
        createRequest(buildModelConfigUrl(), 10000),
        QJsonDocument(payload).toJson(QJsonDocument::Compact));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleModelConfigSaveReply(reply);
    });
}

void BackendClient::testModelConnection(
    const QString &provider,
    const QString &baseUrl,
    const QString &model,
    const QString &thinking,
    const QString &apiKey)
{
    if (provider.trimmed().isEmpty()) {
        emit modelConnectionTestFailed(QStringLiteral("请选择模型供应商。"));
        return;
    }

    QJsonObject payload;
    payload.insert(QStringLiteral("provider"), provider.trimmed());
    payload.insert(QStringLiteral("base_url"), baseUrl.trimmed());
    payload.insert(QStringLiteral("model"), model.trimmed());
    payload.insert(QStringLiteral("thinking"), thinking.isEmpty() ? QStringLiteral("disabled") : thinking);
    if (!apiKey.trimmed().isEmpty()) {
        // 测试请求里可以临时带上表单中的 Key，但不会写入后端持久化配置。
        payload.insert(QStringLiteral("api_key"), apiKey.trimmed());
    }

    QNetworkReply *reply = networkManager_.post(
        createRequest(buildModelTestUrl(), 45000),
        QJsonDocument(payload).toJson(QJsonDocument::Compact));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleModelConnectionTestReply(reply);
    });
}

void BackendClient::requestMcpConnections()
{
    QNetworkReply *reply = networkManager_.get(createRequest(buildMcpConnectionsUrl(), 6000));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        if (reply->error() != QNetworkReply::NoError) {
            const QString message = replyErrorMessage(reply);
            reply->deleteLater();
            emit mcpConnectionsFailed(message);
            return;
        }
        const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
        reply->deleteLater();
        const QJsonArray values = document.object().value(QStringLiteral("connections")).toArray();
        QList<McpConnectionInfo> connections;
        connections.reserve(values.size());
        for (const QJsonValue &value : values) {
            if (!value.isObject()) {
                continue;
            }
            const McpConnectionInfo connection = readMcpConnectionInfo(value.toObject());
            if (!connection.connectionId.isEmpty()) {
                connections.append(connection);
            }
        }
        if (connections.isEmpty()) {
            emit mcpConnectionsFailed(QStringLiteral("MCP 连接响应为空。"));
            return;
        }
        emit mcpConnectionsReceived(connections);
    });
}

void BackendClient::setPublicReferenceMcpEnabled(bool enabled)
{
    const QString action = enabled ? QStringLiteral("enable") : QStringLiteral("disable");
    QNetworkReply *reply = networkManager_.post(
        createRequest(buildPublicReferenceMcpActionUrl(action), 10000), QByteArray{});
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        if (reply->error() != QNetworkReply::NoError) {
            const QString message = replyErrorMessage(reply);
            reply->deleteLater();
            emit mcpConnectionUpdateFailed(message);
            return;
        }
        const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
        reply->deleteLater();
        const QJsonObject payload = document.object();
        const McpConnectionInfo connection = readMcpConnectionInfo(payload.value(QStringLiteral("connection")).toObject());
        if (connection.connectionId.isEmpty()) {
            emit mcpConnectionUpdateFailed(QStringLiteral("MCP 连接响应缺少状态。"));
            return;
        }
        emit mcpConnectionUpdated(connection, payload.value(QStringLiteral("message")).toString());
    });
}

void BackendClient::testPublicReferenceMcpConnection()
{
    QNetworkReply *reply = networkManager_.post(
        createRequest(buildPublicReferenceMcpActionUrl(QStringLiteral("test")), 10000), QByteArray{});
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        if (reply->error() != QNetworkReply::NoError) {
            const QString message = replyErrorMessage(reply);
            reply->deleteLater();
            emit mcpConnectionUpdateFailed(message);
            return;
        }
        const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
        reply->deleteLater();
        const QJsonObject payload = document.object();
        const McpConnectionInfo connection = readMcpConnectionInfo(payload.value(QStringLiteral("connection")).toObject());
        if (connection.connectionId.isEmpty()) {
            emit mcpConnectionUpdateFailed(QStringLiteral("MCP 检测响应缺少连接状态。"));
            return;
        }
        emit mcpConnectionUpdated(connection, payload.value(QStringLiteral("message")).toString());
    });
}

void BackendClient::importWorkspaceDocument(const QString &filename, const QString &content)
{
    if (filename.trimmed().isEmpty()) {
        emit workspaceDocumentImportFailed(QStringLiteral("文件名为空，无法导入 workspace。"));
        return;
    }

    QJsonObject payload;
    payload.insert(QStringLiteral("filename"), filename.trimmed());
    payload.insert(QStringLiteral("content"), content);

    // 文档导入只写受控 workspace，不触发模型调用；超时保持短一些，避免 UI 长时间等待。
    QNetworkReply *reply = networkManager_.post(
        createRequest(buildWorkspaceDocumentsUrl(), 15000),
        QJsonDocument(payload).toJson(QJsonDocument::Compact));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleWorkspaceDocumentImportReply(reply);
    });
}

void BackendClient::importWorkspaceBinaryDocument(const QString &filename, const QByteArray &content)
{
    if (filename.trimmed().isEmpty()) {
        emit workspaceDocumentImportFailed(QStringLiteral("文件名为空，无法导入 workspace。"));
        return;
    }
    if (content.isEmpty()) {
        emit workspaceDocumentImportFailed(QStringLiteral("二进制文档为空，无法导入 workspace。"));
        return;
    }

    QJsonObject payload;
    payload.insert(QStringLiteral("filename"), filename.trimmed());
    // JSON 不能安全承载原始字节。这里仅做传输编码，不在 Qt 端解析 PDF/DOCX，避免客户端
    // 与后端出现两套提取逻辑、两套页码规则。
    payload.insert(QStringLiteral("content_base64"), QString::fromLatin1(content.toBase64()));

    QNetworkReply *reply = networkManager_.post(
        createRequest(buildWorkspaceDocumentsUrl(), 30000),
        QJsonDocument(payload).toJson(QJsonDocument::Compact));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleWorkspaceDocumentImportReply(reply);
    });
}

void BackendClient::requestWorkspaceDocuments()
{
    // 这个清单同时服务于启动完成、文档助手、PDF 工作区和导入完成回调。它们只需要同一份
    // 轻量元数据，因此把并发 GET 合并为一条：最终信号会广播给所有页面，避免旧请求晚到后
    // 覆盖刚导入材料的状态。这里不缓存结果，也不解析任何文档正文。
    if (workspaceDocumentsRequestInFlight_) {
        return;
    }
    workspaceDocumentsRequestInFlight_ = true;
    QNetworkReply *reply = networkManager_.get(createRequest(buildWorkspaceDocumentsUrl(), 6000));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        // 先释放飞行标记，再交给页面槽函数；槽函数可能因导入成功而立即请求下一次清单。
        workspaceDocumentsRequestInFlight_ = false;
        handleWorkspaceDocumentsReply(reply);
    });
}

void BackendClient::requestKnowledgeBases()
{
    QNetworkReply *reply = networkManager_.get(createRequest(buildKnowledgeBasesUrl(), 6000));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleKnowledgeBasesReply(reply);
    });
}

void BackendClient::createKnowledgeBase(const QString &name, const QString &description)
{
    if (name.trimmed().isEmpty()) {
        emit knowledgeBaseCreateFailed(QStringLiteral("请先输入资料库名称。"));
        return;
    }
    QJsonObject payload;
    payload.insert(QStringLiteral("name"), name.trimmed());
    payload.insert(QStringLiteral("description"), description.trimmed());
    QNetworkReply *reply = networkManager_.post(
        createRequest(buildKnowledgeBasesUrl(), 10000), QJsonDocument(payload).toJson(QJsonDocument::Compact));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleKnowledgeBaseCreateReply(reply);
    });
}

void BackendClient::requestKnowledgeDocuments(const QString &knowledgeBaseId)
{
    if (knowledgeBaseId.trimmed().isEmpty()) {
        emit knowledgeDocumentsFailed(QStringLiteral("请先选择一个资料库。"));
        return;
    }
    QNetworkReply *reply = networkManager_.get(
        createRequest(buildKnowledgeBaseDocumentsUrl(knowledgeBaseId.trimmed()), 10000));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleKnowledgeDocumentsReply(reply);
    });
}

void BackendClient::importWorkspaceDocumentsToKnowledgeBase(
    const QString &knowledgeBaseId, const QStringList &documentNames)
{
    if (knowledgeBaseId.trimmed().isEmpty() || documentNames.isEmpty()) {
        emit knowledgeDocumentsImportFailed(QStringLiteral("请选择资料库和至少一份已导入材料。"));
        return;
    }
    QJsonArray names;
    for (const QString &name : documentNames) {
        if (!name.trimmed().isEmpty()) {
            names.append(name.trimmed());
        }
    }
    if (names.isEmpty()) {
        emit knowledgeDocumentsImportFailed(QStringLiteral("材料引用为空，无法导入资料库。"));
        return;
    }
    QJsonObject payload;
    payload.insert(QStringLiteral("knowledge_base_id"), knowledgeBaseId.trimmed());
    payload.insert(QStringLiteral("workspace_document_names"), names);
    QNetworkReply *reply = networkManager_.post(
        createRequest(buildKnowledgeDocumentsUrl(), 30000), QJsonDocument(payload).toJson(QJsonDocument::Compact));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleKnowledgeDocumentImportReply(reply);
    });
}

void BackendClient::startKnowledgeIndex(const QString &knowledgeBaseId)
{
    if (knowledgeBaseId.trimmed().isEmpty()) {
        emit knowledgeIndexStartFailed(QStringLiteral("请先选择一个资料库。"));
        return;
    }
    QNetworkReply *reply = networkManager_.post(
        createRequest(buildKnowledgeIndexStartUrl(knowledgeBaseId.trimmed()), 15000), QByteArray{});
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleKnowledgeIndexStartReply(reply);
    });
}

void BackendClient::requestKnowledgeIndexJob(const QString &indexJobId)
{
    if (indexJobId.trimmed().isEmpty()) {
        emit knowledgeIndexJobFailed(QStringLiteral("索引任务标识为空。"));
        return;
    }
    QNetworkReply *reply = networkManager_.get(
        createRequest(buildKnowledgeIndexJobUrl(indexJobId.trimmed()), 10000));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleKnowledgeIndexJobReply(reply);
    });
}

void BackendClient::requestKnowledgeVectorCapability()
{
    QNetworkReply *reply = networkManager_.get(createRequest(buildKnowledgeVectorCapabilityUrl(), 6000));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleKnowledgeVectorCapabilityReply(reply);
    });
}

void BackendClient::prepareKnowledgeVectorModel()
{
    const QJsonObject payload{{QStringLiteral("confirm_download"), true}};
    // 首次下载约 91MB 权重，低速网络可能需要更久；页面已展示确认框，网络请求仍异步。
    QNetworkReply *reply = networkManager_.post(
        createRequest(buildKnowledgeVectorPrepareUrl(), 180000), QJsonDocument(payload).toJson(QJsonDocument::Compact));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleKnowledgeVectorPrepareReply(reply);
    });
}

void BackendClient::requestKnowledgeOcrCapability()
{
    QNetworkReply *reply = networkManager_.get(createRequest(buildKnowledgeOcrCapabilityUrl(), 6000));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleKnowledgeOcrCapabilityReply(reply);
    });
}

void BackendClient::prepareKnowledgeOcrModel()
{
    const QJsonObject payload{{QStringLiteral("confirm_download"), true}};
    // K7.4 后端会立即返回 preparation ID，再由 Qt 轮询真实阶段；不能把模型下载时间塞进
    // 一个 HTTP 请求超时，也不能在导入材料的路径中复用这个方法。
    QNetworkReply *reply = networkManager_.post(
        createRequest(buildKnowledgeOcrPrepareUrl(), 15000), QJsonDocument(payload).toJson(QJsonDocument::Compact));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleKnowledgeOcrPrepareReply(reply);
    });
}

void BackendClient::requestKnowledgeOcrPreparation(const QString &preparationId)
{
    if (preparationId.trimmed().isEmpty()) {
        emit knowledgeOcrPreparationFailed(QStringLiteral("OCR 准备任务标识为空。"));
        return;
    }
    QNetworkReply *reply = networkManager_.get(
        createRequest(buildKnowledgeOcrPreparationUrl(preparationId.trimmed()), 10000));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleKnowledgeOcrPreparationReply(reply);
    });
}

void BackendClient::deleteKnowledgeBase(const QString &knowledgeBaseId)
{
    if (knowledgeBaseId.trimmed().isEmpty()) {
        emit knowledgeBaseDeletionFailed(QStringLiteral("请先选择一个资料库。"));
        return;
    }
    QNetworkReply *reply = networkManager_.deleteResource(
        createRequest(buildKnowledgeBaseUrl(knowledgeBaseId.trimmed()), 15000));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleKnowledgeBaseDeletionReply(reply);
    });
}

void BackendClient::startKnowledgeAnswer(const QString &knowledgeBaseId, const QString &query)
{
    if (knowledgeBaseId.trimmed().isEmpty()) {
        emit knowledgeAnswerFailed(QStringLiteral("请先选择一个资料库。"));
        return;
    }
    if (query.trimmed().isEmpty()) {
        emit knowledgeAnswerFailed(QStringLiteral("请输入想从资料中确认的问题。"));
        return;
    }

    const QJsonObject payload{
        {QStringLiteral("knowledge_base_id"), knowledgeBaseId.trimmed()},
        {QStringLiteral("query"), query.trimmed()},
    };
    // 受理接口只返回 task_id。模型调用、引用核验与任务持久化均在后端后台完成，避免 UI 假死。
    QNetworkReply *reply = networkManager_.post(
        createRequest(buildKnowledgeAnswerStartUrl(), 10000), QJsonDocument(payload).toJson(QJsonDocument::Compact));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleKnowledgeAnswerStartReply(reply);
    });
}

void BackendClient::requestKnowledgeAnswerResult(const QString &taskId)
{
    if (taskId.trimmed().isEmpty()) {
        emit knowledgeAnswerFailed(QStringLiteral("知识库问答任务 ID 为空，无法读取结果。"));
        return;
    }
    QNetworkReply *reply = networkManager_.get(
        createRequest(buildKnowledgeAnswerResultUrl(taskId.trimmed()), 10000));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleKnowledgeAnswerResultReply(reply);
    });
}

void BackendClient::startKnowledgeDeepTask(
    const QString &knowledgeBaseId,
    const QString &taskKind,
    const QString &taskGoal,
    const QStringList &documentIds)
{
    if (knowledgeBaseId.trimmed().isEmpty()) {
        emit knowledgeDeepTaskFailed(QStringLiteral("请先选择一个资料库。"));
        return;
    }
    if (taskKind.trimmed().isEmpty() || taskGoal.trimmed().size() < 2) {
        emit knowledgeDeepTaskFailed(QStringLiteral("请选择分析方式，并写明需要完成的目标。"));
        return;
    }

    const QJsonObject payload{
        {QStringLiteral("knowledge_base_id"), knowledgeBaseId.trimmed()},
        {QStringLiteral("task_kind"), taskKind.trimmed()},
        {QStringLiteral("task_goal"), taskGoal.trimmed()},
        {QStringLiteral("document_ids"), QJsonArray::fromStringList(documentIds)},
    };
    // 启动接口仅冻结 scope 并受理后台任务；Map/Reduce 由后端 Runtime 异步执行，Qt 主线程不等待。
    QNetworkReply *reply = networkManager_.post(
        createRequest(buildKnowledgeDeepTaskStartUrl(), 10000), QJsonDocument(payload).toJson(QJsonDocument::Compact));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleKnowledgeDeepTaskStartReply(reply);
    });
}

void BackendClient::requestKnowledgeDeepTaskResult(const QString &taskId)
{
    if (taskId.trimmed().isEmpty()) {
        emit knowledgeDeepTaskFailed(QStringLiteral("深度任务 ID 为空，无法读取结果。"));
        return;
    }
    QNetworkReply *reply = networkManager_.get(
        createRequest(buildKnowledgeDeepTaskResultUrl(taskId.trimmed()), 10000));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleKnowledgeDeepTaskResultReply(reply);
    });
}

void BackendClient::pauseKnowledgeDeepTask(const QString &taskId)
{
    if (taskId.trimmed().isEmpty()) {
        emit knowledgeDeepTaskFailed(QStringLiteral("深度任务 ID 为空，无法暂停。"));
        return;
    }
    QNetworkReply *reply = networkManager_.post(
        createRequest(buildKnowledgeDeepTaskControlUrl(taskId.trimmed(), QStringLiteral("pause")), 10000), QByteArray());
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleKnowledgeDeepTaskControlReply(reply);
    });
}

void BackendClient::resumeKnowledgeDeepTask(const QString &taskId)
{
    if (taskId.trimmed().isEmpty()) {
        emit knowledgeDeepTaskFailed(QStringLiteral("深度任务 ID 为空，无法继续。"));
        return;
    }
    QNetworkReply *reply = networkManager_.post(
        createRequest(buildKnowledgeDeepTaskControlUrl(taskId.trimmed(), QStringLiteral("resume")), 10000), QByteArray());
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleKnowledgeDeepTaskControlReply(reply);
    });
}

void BackendClient::cancelKnowledgeDeepTask(const QString &taskId)
{
    if (taskId.trimmed().isEmpty()) {
        emit knowledgeDeepTaskFailed(QStringLiteral("深度任务 ID 为空，无法取消。"));
        return;
    }
    QNetworkReply *reply = networkManager_.post(
        createRequest(buildKnowledgeDeepTaskControlUrl(taskId.trimmed(), QStringLiteral("cancel")), 10000), QByteArray());
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleKnowledgeDeepTaskControlReply(reply);
    });
}

void BackendClient::exportKnowledgeDeepTaskReport(const QString &taskId, const QString &filename)
{
    if (taskId.trimmed().isEmpty()) {
        emit knowledgeDeepTaskFailed(QStringLiteral("深度任务 ID 为空，无法导出正式报告。"));
        return;
    }
    QJsonObject payload{{QStringLiteral("confirmed"), true}};
    if (!filename.trimmed().isEmpty()) {
        payload.insert(QStringLiteral("filename"), filename.trimmed());
    }
    QNetworkReply *reply = networkManager_.post(
        createRequest(buildKnowledgeDeepTaskReportUrl(taskId.trimmed()), 10000),
        QJsonDocument(payload).toJson(QJsonDocument::Compact));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleKnowledgeDeepTaskReportReply(reply);
    });
}

void BackendClient::importDataDataset(const QString &filename, const QByteArray &content)
{
    if (filename.trimmed().isEmpty()) {
        emit dataDatasetImportFailed(QStringLiteral("文件名为空，无法导入数据工作区。"));
        return;
    }
    if (content.isEmpty()) {
        emit dataDatasetImportFailed(QStringLiteral("数据文件为空，无法建立数据画像。"));
        return;
    }

    QJsonObject payload;
    payload.insert(QStringLiteral("filename"), filename.trimmed());
    payload.insert(QStringLiteral("content_base64"), QString::fromLatin1(content.toBase64()));
    // 20MB 文件的传输加解析可比普通文档更久；真正解析仍由后端线程池完成，Qt 主线程不等待。
    QNetworkReply *reply = networkManager_.post(
        createRequest(buildDataAgentDatasetsUrl(), 45000),
        QJsonDocument(payload).toJson(QJsonDocument::Compact));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleDataDatasetImportReply(reply);
    });
}

void BackendClient::requestDataDatasets()
{
    // 数据列表只由页面打开、导入成功或用户主动刷新触发，不做自动轮询和整表扫描。
    QNetworkReply *reply = networkManager_.get(createRequest(buildDataAgentDatasetsUrl(), 6000));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleDataDatasetsReply(reply);
    });
}

void BackendClient::requestDataDatasetProfile(const QString &datasetName)
{
    if (datasetName.trimmed().isEmpty()) {
        emit dataDatasetProfileFailed(QStringLiteral("请先选择一个 Excel 或 CSV 文件。"));
        return;
    }
    // 画像可能首次读取 20MB 源文件；后端按文件版本缓存，Qt 仅异步等待一次结果。
    QNetworkReply *reply = networkManager_.get(
        createRequest(buildDataAgentDatasetProfileUrl(datasetName.trimmed()), 60000));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleDataDatasetProfileReply(reply);
    });
}

void BackendClient::requestDataRecommendations(const QString &datasetName, const QString &goal)
{
    if (datasetName.trimmed().isEmpty()) {
        emit dataRecommendationsFailed(QStringLiteral("请先选择并完成画像的一份数据文件。"));
        return;
    }

    QJsonObject payload;
    payload.insert(QStringLiteral("dataset_name"), datasetName.trimmed());
    payload.insert(QStringLiteral("goal"), goal.trimmed());
    // 推荐只复用后端 L1 画像缓存，网络请求短于 D2 聚合预览；整个回调仍保持在 Qt 异步链上。
    QNetworkReply *reply = networkManager_.post(
        createRequest(buildDataAgentRecommendationsUrl(), 15000),
        QJsonDocument(payload).toJson(QJsonDocument::Compact));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleDataRecommendationsReply(reply);
    });
}

void BackendClient::requestDataAnalysisPreview(const QString &datasetName, const QString &goal, int maxChartCount)
{
    if (datasetName.trimmed().isEmpty()) {
        emit dataAnalysisPreviewFailed(QStringLiteral("请先选择并完成画像的一份数据文件。"));
        return;
    }

    QJsonObject payload;
    payload.insert(QStringLiteral("dataset_name"), datasetName.trimmed());
    payload.insert(QStringLiteral("goal"), goal.trimmed());
    payload.insert(QStringLiteral("cleaning_policy"), QStringLiteral("safe"));
    payload.insert(QStringLiteral("max_chart_count"), qBound(1, maxChartCount, 4));
    // D2 最多重新读取一个 20MB 受控副本并执行有限聚合。它不会等待外部 Provider，但仍给
    // 低性能磁盘足够的时间；Qt 网络层全程异步，主窗口不会被 DataFrame 计算卡住。
    QNetworkReply *reply = networkManager_.post(
        createRequest(buildDataAgentAnalysisPreviewUrl(), 90000),
        QJsonDocument(payload).toJson(QJsonDocument::Compact));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleDataAnalysisPreviewReply(reply);
    });
}

void BackendClient::requestDataAnalysisWorkbookExport(
    const QString &datasetName,
    const QString &sourceSha256,
    const QString &goal,
    int maxChartCount)
{
    if (datasetName.trimmed().isEmpty() || sourceSha256.trimmed().size() != 64) {
        emit dataAnalysisWorkbookExportFailed(QStringLiteral("分析预览已失效，请重新生成后再导出。"));
        return;
    }

    QJsonObject payload;
    payload.insert(QStringLiteral("dataset_name"), datasetName.trimmed());
    payload.insert(QStringLiteral("source_sha256"), sourceSha256.trimmed());
    payload.insert(QStringLiteral("goal"), goal.trimmed());
    payload.insert(QStringLiteral("cleaning_policy"), QStringLiteral("safe"));
    payload.insert(QStringLiteral("max_chart_count"), qBound(1, maxChartCount, 4));
    payload.insert(QStringLiteral("confirmed"), true);
    // D4 的 /start 只负责受理。真正导出、回读和 artifact 落库在后台任务中进行，Qt 可立即
    // 建立实时日志连接；这避免低速磁盘或较大文件被误显示成“界面卡住”。
    QNetworkReply *reply = networkManager_.post(
        createRequest(buildDataAgentAnalysisExportStartUrl(), 10000),
        QJsonDocument(payload).toJson(QJsonDocument::Compact));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleDataAnalysisWorkbookExportStartReply(reply);
    });
}

void BackendClient::requestDataAnalysisWorkbookExportResult(const QString &taskId)
{
    if (taskId.trimmed().isEmpty()) {
        emit dataAnalysisWorkbookExportFailed(QStringLiteral("数据导出任务 ID 为空，无法读取验证结果。"));
        return;
    }
    QNetworkReply *reply = networkManager_.get(
        createRequest(buildDataAgentAnalysisExportResultUrl(taskId.trimmed()), 10000));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleDataAnalysisWorkbookExportResultReply(reply);
    });
}

void BackendClient::requestDataChartExport(
    const QString &datasetName,
    const QString &sourceSha256,
    const QString &goal,
    int maxChartCount)
{
    if (datasetName.trimmed().isEmpty() || sourceSha256.trimmed().size() != 64) {
        emit dataChartExportFailed(QStringLiteral("分析预览已失效，请重新生成后再保存图表。"));
        return;
    }

    QJsonObject payload;
    payload.insert(QStringLiteral("dataset_name"), datasetName.trimmed());
    payload.insert(QStringLiteral("source_sha256"), sourceSha256.trimmed());
    payload.insert(QStringLiteral("goal"), goal.trimmed());
    payload.insert(QStringLiteral("cleaning_policy"), QStringLiteral("safe"));
    payload.insert(QStringLiteral("max_chart_count"), qBound(1, maxChartCount, 4));
    payload.insert(QStringLiteral("confirmed"), true);
    // 与 Excel 导出相同，HTTP 只负责受理；绘图和 PNG 回读都留给后台与事件流，Qt 主线程不等待。
    QNetworkReply *reply = networkManager_.post(
        createRequest(buildDataAgentChartExportStartUrl(), 10000),
        QJsonDocument(payload).toJson(QJsonDocument::Compact));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleDataChartExportStartReply(reply);
    });
}

void BackendClient::requestDataChartExportResult(const QString &taskId)
{
    if (taskId.trimmed().isEmpty()) {
        emit dataChartExportFailed(QStringLiteral("图表任务 ID 为空，无法读取交付结果。"));
        return;
    }
    QNetworkReply *reply = networkManager_.get(
        createRequest(buildDataAgentChartExportResultUrl(taskId.trimmed()), 10000));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleDataChartExportResultReply(reply);
    });
}

void BackendClient::requestDataChartImage(const QString &taskId, const QString &artifactId)
{
    if (taskId.trimmed().isEmpty() || artifactId.trimmed().isEmpty()) {
        emit dataChartImageFailed(taskId, artifactId, QStringLiteral("图表产物标识为空。"));
        return;
    }
    QNetworkReply *reply = networkManager_.get(
        createRequest(buildDataAgentChartImageUrl(taskId.trimmed(), artifactId.trimmed()), 30000));
    connect(reply, &QNetworkReply::finished, this, [this, reply, taskId, artifactId]() {
        if (reply->error() != QNetworkReply::NoError) {
            const QString message = replyErrorMessage(reply);
            reply->deleteLater();
            emit dataChartImageFailed(taskId, artifactId, message);
            return;
        }
        const QByteArray imageBytes = reply->readAll();
        reply->deleteLater();
        if (imageBytes.isEmpty()) {
            emit dataChartImageFailed(taskId, artifactId, QStringLiteral("图表图片响应为空。"));
            return;
        }
        emit dataChartImageReceived(taskId, artifactId, imageBytes);
    });
}

void BackendClient::requestDataTransformationPreview(const QJsonObject &request)
{
    const QString datasetName = request.value(QStringLiteral("dataset_name")).toString().trimmed();
    const QString sourceSha256 = request.value(QStringLiteral("source_sha256")).toString().trimmed();
    if (datasetName.isEmpty() || sourceSha256.size() != 64) {
        emit dataTransformationPreviewFailed(QStringLiteral("当前数据版本无效，请重新建立画像后再加工。"));
        return;
    }
    QNetworkReply *reply = networkManager_.post(
        createRequest(buildDataAgentTransformationPreviewUrl(), 60000),
        QJsonDocument(request).toJson(QJsonDocument::Compact));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleDataTransformationPreviewReply(reply);
    });
}

void BackendClient::requestDataTransformationExport(const QJsonObject &request)
{
    const QString datasetName = request.value(QStringLiteral("dataset_name")).toString().trimmed();
    const QString sourceSha256 = request.value(QStringLiteral("source_sha256")).toString().trimmed();
    const QString operation = request.value(QStringLiteral("operation_type")).toString().trimmed();
    const QString primary = request.value(QStringLiteral("primary_column")).toString().trimmed();
    if (datasetName.isEmpty() || sourceSha256.size() != 64 || operation.isEmpty() || primary.isEmpty()) {
        emit dataTransformationExportFailed(QStringLiteral("字段加工预览已失效，请重新生成后再保存副本。"));
        return;
    }
    QJsonObject payload = request;
    payload.insert(QStringLiteral("confirmed"), true);
    QNetworkReply *reply = networkManager_.post(
        createRequest(buildDataAgentTransformationExportStartUrl(), 10000),
        QJsonDocument(payload).toJson(QJsonDocument::Compact));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleDataTransformationExportStartReply(reply);
    });
}

void BackendClient::requestDataTransformationExportResult(const QString &taskId)
{
    if (taskId.trimmed().isEmpty()) {
        emit dataTransformationExportFailed(QStringLiteral("字段加工任务 ID 为空，无法读取交付结果。"));
        return;
    }
    QNetworkReply *reply = networkManager_.get(
        createRequest(buildDataAgentTransformationExportResultUrl(taskId.trimmed()), 10000));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleDataTransformationExportResultReply(reply);
    });
}

void BackendClient::runDocumentAgent(
    const QString &taskGoal,
    const QStringList &documentRefs,
    const QString &outputMode,
    const QString &query)
{
    if (taskGoal.trimmed().isEmpty()) {
        emit documentAgentFailed(QStringLiteral("请先说明希望从文档中获得什么结论。"));
        return;
    }

    QJsonArray documentArray;
    for (const QString &documentRef : documentRefs) {
        if (!documentRef.trimmed().isEmpty()) {
            documentArray.append(documentRef.trimmed());
        }
    }

    QJsonObject payload;
    payload.insert(QStringLiteral("task_goal"), taskGoal.trimmed());
    payload.insert(QStringLiteral("document_refs"), documentArray);
    payload.insert(QStringLiteral("output_mode"), outputMode.isEmpty() ? QStringLiteral("auto") : outputMode);
    if (!query.trimmed().isEmpty()) {
        payload.insert(QStringLiteral("query"), query.trimmed());
    }

    // /start 只负责受理，不能让一次模型调用长期占住 HTTP 响应。结果会在任务日志终态后另取。
    QNetworkReply *reply = networkManager_.post(
        createRequest(buildDocumentAgentStartUrl(), 10000),
        QJsonDocument(payload).toJson(QJsonDocument::Compact));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleDocumentAgentStartReply(reply);
    });
}

void BackendClient::requestDocumentAgentResult(const QString &taskId)
{
    if (taskId.isEmpty()) {
        emit documentAgentFailed(QStringLiteral("文档任务 ID 为空，无法读取分析结果。"));
        return;
    }

    QNetworkReply *reply = networkManager_.get(createRequest(buildDocumentAgentResultUrl(taskId), 10000));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleDocumentAgentResultReply(reply);
    });
}

void BackendClient::startPdfProcessing(
    const QString &operation,
    const QStringList &documentRefs,
    const QString &pageRange,
    int rotationDegrees)
{
    QJsonArray documentArray;
    for (const QString &documentRef : documentRefs) {
        if (!documentRef.trimmed().isEmpty()) {
            documentArray.append(documentRef.trimmed());
        }
    }
    if (operation.trimmed().isEmpty() || documentArray.isEmpty()) {
        emit pdfProcessingFailed(QStringLiteral("请选择 PDF 文件和处理方式后再继续。"));
        return;
    }

    QJsonObject payload;
    payload.insert(QStringLiteral("operation"), operation.trimmed());
    payload.insert(QStringLiteral("document_refs"), documentArray);
    if (!pageRange.trimmed().isEmpty()) {
        payload.insert(QStringLiteral("page_range"), pageRange.trimmed());
    }
    if (rotationDegrees > 0) {
        payload.insert(QStringLiteral("rotation_degrees"), rotationDegrees);
    }

    // 文件整理可能需要重写多个 PDF 页面；HTTP 只等待受理，不等待真正处理结束。
    QNetworkReply *reply = networkManager_.post(
        createRequest(buildPdfProcessingStartUrl(), 10000),
        QJsonDocument(payload).toJson(QJsonDocument::Compact));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handlePdfProcessingStartReply(reply);
    });
}

void BackendClient::requestPdfProcessingResult(const QString &taskId)
{
    if (taskId.isEmpty()) {
        emit pdfProcessingFailed(QStringLiteral("PDF 任务 ID 为空，无法读取处理结果。"));
        return;
    }
    QNetworkReply *reply = networkManager_.get(createRequest(buildPdfProcessingResultUrl(taskId), 10000));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handlePdfProcessingResultReply(reply);
    });
}

void BackendClient::expandDocumentDraftSection(
    const QString &sourceTaskId,
    const QString &sectionId,
    const QString &instruction)
{
    if (sourceTaskId.trimmed().isEmpty() || sectionId.trimmed().isEmpty()) {
        emit documentAgentFailed(QStringLiteral("原草稿任务或章节信息为空，无法生成本章预览。"));
        return;
    }
    if (instruction.trimmed().isEmpty()) {
        emit documentAgentFailed(QStringLiteral("请说明希望如何调整本章后再继续。"));
        return;
    }

    QJsonObject payload;
    payload.insert(QStringLiteral("section_id"), sectionId.trimmed());
    payload.insert(QStringLiteral("instruction"), instruction.trimmed());
    // 与普通 /start 一样只等待受理回执；真实模型调用和来源校验在后台任务中完成。
    QNetworkReply *reply = networkManager_.post(
        createRequest(buildDocumentDraftSectionStartUrl(sourceTaskId), 10000),
        QJsonDocument(payload).toJson(QJsonDocument::Compact));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleDocumentAgentStartReply(reply);
    });
}

void BackendClient::reviewDocumentDraft(const QString &sourceTaskId, const QString &focus)
{
    if (sourceTaskId.trimmed().isEmpty()) {
        emit documentAgentFailed(QStringLiteral("原草稿任务为空，无法进行事实核验。"));
        return;
    }
    QJsonObject payload;
    payload.insert(QStringLiteral("focus"), focus.trimmed());
    QNetworkReply *reply = networkManager_.post(
        createRequest(buildDocumentDraftReviewStartUrl(sourceTaskId), 10000),
        QJsonDocument(payload).toJson(QJsonDocument::Compact));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() { handleDocumentAgentStartReply(reply); });
}

void BackendClient::reviewDocumentDraftSection(
    const QString &sourceTaskId,
    const QString &sectionId,
    const QString &focus)
{
    if (sourceTaskId.trimmed().isEmpty() || sectionId.trimmed().isEmpty()) {
        emit documentAgentFailed(QStringLiteral("原草稿任务或章节信息为空，无法进行本章审校。"));
        return;
    }

    QJsonObject payload;
    payload.insert(QStringLiteral("section_id"), sectionId.trimmed());
    payload.insert(QStringLiteral("focus"), focus.trimmed());
    // 审校和章节预览一样只等待受理回执；实际读取、模型建议和来源校验均在后端后台任务完成。
    QNetworkReply *reply = networkManager_.post(
        createRequest(buildDocumentDraftSectionReviewStartUrl(sourceTaskId), 10000),
        QJsonDocument(payload).toJson(QJsonDocument::Compact));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleDocumentAgentStartReply(reply);
    });
}

void BackendClient::createDocumentDraftSectionRevisionPreview(
    const QString &sourceReviewTaskId,
    const QString &suggestionId)
{
    if (sourceReviewTaskId.trimmed().isEmpty() || suggestionId.trimmed().isEmpty()) {
        emit documentAgentFailed(QStringLiteral("本章审校任务或建议信息为空，无法生成修订预览。"));
        return;
    }

    QJsonObject payload;
    payload.insert(QStringLiteral("suggestion_id"), suggestionId.trimmed());
    // 这是无模型的精确替换预览：服务端按审校任务恢复候选文本，Qt 不传递原文或文件路径。
    QNetworkReply *reply = networkManager_.post(
        createRequest(buildDocumentDraftSectionRevisionPreviewStartUrl(sourceReviewTaskId), 10000),
        QJsonDocument(payload).toJson(QJsonDocument::Compact));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleDocumentAgentStartReply(reply);
    });
}

void BackendClient::createDocumentDraftSectionBatchRevisionPreview(
    const QString &sourceReviewTaskId,
    const QStringList &suggestionIds)
{
    if (sourceReviewTaskId.trimmed().isEmpty() || suggestionIds.size() < 2 || suggestionIds.size() > 6) {
        emit documentAgentFailed(QStringLiteral("请选择 2 至 6 条审校建议后再生成合并预览。"));
        return;
    }

    QJsonArray selectedIds;
    QSet<QString> uniqueIds;
    for (const QString &suggestionId : suggestionIds) {
        const QString normalizedId = suggestionId.trimmed();
        if (normalizedId.isEmpty() || uniqueIds.contains(normalizedId)) {
            emit documentAgentFailed(QStringLiteral("批量修订建议无效或存在重复项，请重新选择。"));
            return;
        }
        uniqueIds.insert(normalizedId);
        selectedIds.append(normalizedId);
    }

    QJsonObject payload;
    payload.insert(QStringLiteral("suggestion_ids"), selectedIds);
    // Qt 只传任务身份和勾选 ID；候选正文、章节和冲突检测全部由后端从审校快照重新恢复。
    QNetworkReply *reply = networkManager_.post(
        createRequest(buildDocumentDraftSectionBatchRevisionPreviewStartUrl(sourceReviewTaskId), 10000),
        QJsonDocument(payload).toJson(QJsonDocument::Compact));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleDocumentAgentStartReply(reply);
    });
}

void BackendClient::createDocumentDraftSectionManualRevisionPreview(
    const QString &sourceTaskId,
    const QString &sectionId,
    const QString &revisedBody)
{
    if (sourceTaskId.trimmed().isEmpty() || sectionId.trimmed().isEmpty()) {
        emit documentAgentFailed(QStringLiteral("原草稿任务或章节信息为空，无法建立手动修订预览。"));
        return;
    }
    if (revisedBody.trimmed().isEmpty() || revisedBody.size() > 1500) {
        emit documentAgentFailed(QStringLiteral("手动修订后的章节正文不能为空，且不能超过 1500 个字符。"));
        return;
    }

    QJsonObject payload;
    payload.insert(QStringLiteral("section_id"), sectionId.trimmed());
    payload.insert(QStringLiteral("revised_body"), revisedBody.trimmed());
    // 唯一的自由文本只用于建立待核验预览；服务端不会把它当作已验证结论或文件写入请求。
    QNetworkReply *reply = networkManager_.post(
        createRequest(buildDocumentDraftSectionManualRevisionPreviewStartUrl(sourceTaskId), 10000),
        QJsonDocument(payload).toJson(QJsonDocument::Compact));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleDocumentAgentStartReply(reply);
    });
}

void BackendClient::restoreDocumentDraftPreview(const QString &sourceTaskId)
{
    if (sourceTaskId.trimmed().isEmpty()) {
        emit documentAgentFailed(QStringLiteral("历史草稿任务为空，无法建立恢复预览。"));
        return;
    }

    // 恢复接口不接收正文、文件名或路径：后端只从 sourceTaskId 对应的完成快照复制已验证结果。
    QNetworkReply *reply = networkManager_.post(
        createRequest(buildDocumentDraftRestorePreviewStartUrl(sourceTaskId), 10000),
        QJsonDocument(QJsonObject{}).toJson(QJsonDocument::Compact));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleDocumentAgentStartReply(reply);
    });
}

void BackendClient::createDocumentDraftTemplatePreview(
    const QString &sourceTaskId,
    const QString &templateId)
{
    if (sourceTaskId.trimmed().isEmpty() || templateId.trimmed().isEmpty()) {
        emit documentAgentFailed(QStringLiteral("原草稿任务或交付模板为空，无法建立模板化交付预览。"));
        return;
    }

    QJsonObject payload;
    payload.insert(QStringLiteral("template_id"), templateId.trimmed());
    // 模板化交付不接收正文、文件名或路径；服务端只从已核验草稿快照恢复章节和来源。
    QNetworkReply *reply = networkManager_.post(
        createRequest(buildDocumentDraftTemplatePreviewStartUrl(sourceTaskId), 10000),
        QJsonDocument(payload).toJson(QJsonDocument::Compact));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleDocumentAgentStartReply(reply);
    });
}

void BackendClient::requestDocumentDraftMergeCandidates(const QString &taskId)
{
    if (taskId.trimmed().isEmpty()) {
        emit documentDraftMergeFailed(QStringLiteral("当前草稿任务为空，无法加载可合并版本。"));
        return;
    }

    QNetworkReply *reply = networkManager_.get(
        createRequest(buildDocumentDraftMergeCandidatesUrl(taskId), 10000));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        if (reply->error() != QNetworkReply::NoError) {
            emit documentDraftMergeFailed(replyErrorMessage(reply));
            reply->deleteLater();
            return;
        }

        const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
        reply->deleteLater();
        if (!document.isObject()) {
            emit documentDraftMergeFailed(QStringLiteral("合并候选响应格式无效。"));
            return;
        }
        const QJsonObject result = document.object();
        if (result.value(QStringLiteral("task_id")).toString().isEmpty()
            || result.value(QStringLiteral("root_task_id")).toString().isEmpty()
            || !result.value(QStringLiteral("candidates")).isArray()) {
            emit documentDraftMergeFailed(QStringLiteral("合并候选缺少可展示的版本身份。"));
            return;
        }
        emit documentDraftMergeCandidatesReceived(result);
    });
}

void BackendClient::requestDocumentDraftMergePlan(
    const QString &primaryTaskId,
    const QString &secondaryTaskId)
{
    if (primaryTaskId.trimmed().isEmpty() || secondaryTaskId.trimmed().isEmpty()) {
        emit documentDraftMergeFailed(QStringLiteral("当前草稿或候选版本为空，无法计算章节合并计划。"));
        return;
    }

    QNetworkReply *reply = networkManager_.get(
        createRequest(buildDocumentDraftMergePlanUrl(primaryTaskId, secondaryTaskId), 10000));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        if (reply->error() != QNetworkReply::NoError) {
            emit documentDraftMergeFailed(replyErrorMessage(reply));
            reply->deleteLater();
            return;
        }

        const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
        reply->deleteLater();
        if (!document.isObject()) {
            emit documentDraftMergeFailed(QStringLiteral("章节合并计划响应格式无效。"));
            return;
        }
        const QJsonObject result = document.object();
        if (result.value(QStringLiteral("primary_task_id")).toString().isEmpty()
            || result.value(QStringLiteral("secondary_task_id")).toString().isEmpty()
            || result.value(QStringLiteral("common_ancestor_task_id")).toString().isEmpty()
            || !result.value(QStringLiteral("conflicts")).isArray()) {
            emit documentDraftMergeFailed(QStringLiteral("章节合并计划缺少共同祖先或冲突信息。"));
            return;
        }
        emit documentDraftMergePlanReceived(result);
    });
}

void BackendClient::createDocumentDraftMergePreview(
    const QString &primaryTaskId,
    const QString &secondaryTaskId,
    const QJsonArray &resolutions)
{
    if (primaryTaskId.trimmed().isEmpty() || secondaryTaskId.trimmed().isEmpty()) {
        emit documentAgentFailed(QStringLiteral("当前草稿或候选版本为空，无法建立章节合并预览。"));
        return;
    }

    QJsonObject payload;
    payload.insert(QStringLiteral("other_task_id"), secondaryTaskId.trimmed());
    payload.insert(QStringLiteral("resolutions"), resolutions);
    // Qt 只提交另一版本身份和已展示冲突的选择；正文、共同祖先、来源与最终合并仍由后端恢复。
    QNetworkReply *reply = networkManager_.post(
        createRequest(buildDocumentDraftMergePreviewStartUrl(primaryTaskId), 10000),
        QJsonDocument(payload).toJson(QJsonDocument::Compact));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleDocumentAgentStartReply(reply);
    });
}

void BackendClient::requestDocumentDraftParentDiff(const QString &taskId)
{
    if (taskId.trimmed().isEmpty()) {
        emit documentDraftParentDiffFailed(QStringLiteral("当前草稿任务为空，无法比较版本。"));
        return;
    }

    QNetworkReply *reply = networkManager_.get(
        createRequest(buildDocumentDraftParentDiffUrl(taskId), 10000));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        if (reply->error() != QNetworkReply::NoError) {
            emit documentDraftParentDiffFailed(replyErrorMessage(reply));
            reply->deleteLater();
            return;
        }

        const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
        reply->deleteLater();
        if (!document.isObject()) {
            emit documentDraftParentDiffFailed(QStringLiteral("版本差异响应格式无效。"));
            return;
        }
        const QJsonObject result = document.object();
        if (result.value(QStringLiteral("task_id")).toString().isEmpty()
            || result.value(QStringLiteral("parent_task_id")).toString().isEmpty()
            || !result.value(QStringLiteral("sections")).isArray()) {
            emit documentDraftParentDiffFailed(QStringLiteral("版本差异缺少可展示的快照信息。"));
            return;
        }
        emit documentDraftParentDiffReceived(result);
    });
}

void BackendClient::saveDocumentDraft(const QString &taskId, const QString &filename)
{
    if (taskId.isEmpty()) {
        emit documentDraftSaveFailed(QStringLiteral("文档任务 ID 为空，无法保存草稿。"));
        return;
    }

    QJsonObject payload;
    payload.insert(QStringLiteral("filename"), filename.trimmed());
    // Qt 的确认对话框已经由用户点选；协议仍显式带上确认字段，防止预览请求误变成写入请求。
    payload.insert(QStringLiteral("confirmed"), true);
    QNetworkReply *reply = networkManager_.post(
        createRequest(buildDocumentDraftSaveUrl(taskId), 15000),
        QJsonDocument(payload).toJson(QJsonDocument::Compact));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleDocumentDraftSaveReply(reply);
    });
}

void BackendClient::requestProjectProposalPresentationPreview(const QString &taskId)
{
    if (taskId.trimmed().isEmpty()) {
        emit presentationPreviewFailed(QStringLiteral("当前草稿任务为空，无法生成项目方案 PPT 计划。"));
        return;
    }

    QJsonObject payload;
    payload.insert(QStringLiteral("presentation_type"), QStringLiteral("project_proposal"));
    QNetworkReply *reply = networkManager_.post(
        createRequest(buildPresentationPreviewUrl(taskId), 15000),
        QJsonDocument(payload).toJson(QJsonDocument::Compact));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handlePresentationPreviewReply(reply);
    });
}

void BackendClient::exportProjectProposalPresentation(
    const QString &taskId,
    const QString &planId,
    const QString &filename)
{
    if (taskId.trimmed().isEmpty() || planId.trimmed().isEmpty()) {
        emit presentationExportFailed(QStringLiteral("项目方案 PPT 计划为空，请重新打开预览后再确认导出。"));
        return;
    }

    QJsonObject payload;
    payload.insert(QStringLiteral("presentation_type"), QStringLiteral("project_proposal"));
    payload.insert(QStringLiteral("plan_id"), planId.trimmed());
    payload.insert(QStringLiteral("filename"), filename.trimmed());
    // confirmed 只能由预览对话框的明确操作发送，主页面没有“直接导出”快捷路径。
    payload.insert(QStringLiteral("confirmed"), true);
    QNetworkReply *reply = networkManager_.post(
        createRequest(buildPresentationExportUrl(taskId), 30000),
        QJsonDocument(payload).toJson(QJsonDocument::Compact));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handlePresentationExportReply(reply);
    });
}

void BackendClient::startPresentationStudio(
    const QString &intent,
    int targetSlideCount,
    const QString &visualAssetProvider,
    bool publicResearchEnabled,
    bool structuredDataEnabled)
{
    if (intent.trimmed().size() < 4) {
        emit presentationStudioFailed(QStringLiteral("请用一句话说明你想制作什么演示文稿。"));
        return;
    }

    QJsonObject payload;
    payload.insert(QStringLiteral("intent"), intent.trimmed());
    payload.insert(QStringLiteral("target_slide_count"), qBound(0, targetSlideCount, 12));
    const QString normalizedProvider = visualAssetProvider == QStringLiteral("pexels")
        || visualAssetProvider == QStringLiteral("seedream")
        ? visualAssetProvider
        : QStringLiteral("none");
    // 该字段只让计划生成受控页面视觉意图；计划阶段仍完全不联网，也不调用图像模型。
    payload.insert(QStringLiteral("visual_asset_provider"), normalizedProvider);
    // 保留 V2.1 后端和历史调试客户端兼容字段；新协议以 visual_asset_provider 为准。
    payload.insert(QStringLiteral("allow_licensed_assets"), normalizedProvider == QStringLiteral("pexels"));
    // 公开资料的意图也进入计划身份；计划阶段不访问任何外部接口。
    payload.insert(QStringLiteral("public_research_enabled"), publicResearchEnabled);
    // 图表计划同样在本地完成意图识别；真正的数据请求只会发生在确认导出阶段。
    payload.insert(QStringLiteral("structured_data_enabled"), structuredDataEnabled);
    QNetworkReply *reply = networkManager_.post(
        createRequest(buildPresentationStudioStartUrl(), 15000),
        QJsonDocument(payload).toJson(QJsonDocument::Compact));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handlePresentationStudioStartReply(reply);
    });
}

void BackendClient::requestPresentationStudioResult(const QString &taskId)
{
    if (taskId.trimmed().isEmpty()) {
        emit presentationStudioFailed(QStringLiteral("PPT 创作任务为空，无法读取计划。"));
        return;
    }
    QNetworkReply *reply = networkManager_.get(
        createRequest(buildPresentationStudioResultUrl(taskId), 15000));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handlePresentationStudioResultReply(reply);
    });
}

void BackendClient::exportPresentationStudio(
    const QString &taskId,
    const QString &planId,
    const QString &filename,
    bool fetchExternalAssets,
    bool fetchPublicResearch,
    bool fetchStructuredData,
    bool networkConfirmed)
{
    if (taskId.trimmed().isEmpty() || planId.trimmed().isEmpty()) {
        emit presentationStudioExportFailed(QStringLiteral("PPT 创作计划为空，请重新生成后再确认导出。"));
        return;
    }

    QJsonObject payload;
    payload.insert(QStringLiteral("plan_id"), planId.trimmed());
    payload.insert(QStringLiteral("filename"), filename.trimmed());
    // 文件写入只能来自独立工作台的明确确认；后端仍会二次检查此字段与计划身份。
    payload.insert(QStringLiteral("confirmed"), true);
    // 图库检索和生成式图片都是另一项外部副作用，不能从“确认写入 PPTX”中静默推断。
    payload.insert(QStringLiteral("fetch_external_assets"), fetchExternalAssets);
    // 保留旧字段，确保升级期间仍能和 V2.1 服务端兼容。
    payload.insert(QStringLiteral("fetch_licensed_assets"), fetchExternalAssets);
    payload.insert(QStringLiteral("fetch_public_research"), fetchPublicResearch);
    payload.insert(QStringLiteral("fetch_structured_data"), fetchStructuredData);
    payload.insert(QStringLiteral("network_confirmed"), networkConfirmed);
    QNetworkReply *reply = networkManager_.post(
        // Seedream 最多四张串行生成，每张允许三次有限尝试。最坏情况下 4 * 3 * 75 秒，
        // 再加写入/回读余量；Qt 请求保持异步，宁可持续展示阶段状态，也不能让后端已写出
        // PPTX 后客户端先误报超时。
        createRequest(buildPresentationStudioExportUrl(taskId), 990000),
        QJsonDocument(payload).toJson(QJsonDocument::Compact));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handlePresentationStudioExportReply(reply);
    });
}

void BackendClient::preparePresentationStudioExport(const QString &taskId)
{
    if (taskId.trimmed().isEmpty()) {
        emit presentationStudioExportFailed(QStringLiteral("PPT 创作计划为空，请重新生成后再确认导出。"));
        return;
    }

    // 此请求只创建新的实时状态缓冲，不会联网、写文件或消耗图片/模型额度。Qt 收到 202 后
    // 再连接 WebSocket 并提交导出，避免旧计划阶段的已关闭连接抢走本次导出日志。
    QNetworkReply *reply = networkManager_.post(
        createRequest(buildPresentationStudioExportPrepareUrl(taskId)),
        QByteArray());
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handlePresentationStudioExportPrepareReply(reply);
    });
}

void BackendClient::requestProjectDocumentReview(const QString &documentRef, const QString &documentType)
{
    if (documentRef.trimmed().isEmpty()) {
        emit projectReviewFailed(QStringLiteral("请选择需要审查的项目材料。"));
        return;
    }

    QJsonObject payload;
    payload.insert(QStringLiteral("document_ref"), documentRef.trimmed());
    payload.insert(QStringLiteral("document_type"), documentType.trimmed().isEmpty()
        ? QStringLiteral("auto")
        : documentType.trimmed());
    QNetworkReply *reply = networkManager_.post(
        createRequest(buildProjectReviewRunUrl(), 30000),
        QJsonDocument(payload).toJson(QJsonDocument::Compact));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleProjectReviewReply(reply);
    });
}

void BackendClient::requestPaperReview(const QString &documentRef, const QString &paperType)
{
    if (documentRef.trimmed().isEmpty()) {
        emit paperReviewFailed(QStringLiteral("请选择需要审查的论文或学术报告。"));
        return;
    }

    QJsonObject payload;
    payload.insert(QStringLiteral("document_ref"), documentRef.trimmed());
    payload.insert(QStringLiteral("paper_type"), paperType.trimmed().isEmpty()
        ? QStringLiteral("auto")
        : paperType.trimmed());
    QNetworkReply *reply = networkManager_.post(
        createRequest(buildPaperReviewRunUrl(), 30000),
        QJsonDocument(payload).toJson(QJsonDocument::Compact));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handlePaperReviewReply(reply);
    });
}

void BackendClient::startProjectDocumentReview(const QString &documentRef, const QString &documentType)
{
    if (documentRef.trimmed().isEmpty()) {
        emit projectReviewFailed(QStringLiteral("请选择需要审查的项目材料。"));
        return;
    }

    QJsonObject payload;
    payload.insert(QStringLiteral("document_ref"), documentRef.trimmed());
    payload.insert(QStringLiteral("document_type"), documentType.trimmed().isEmpty()
        ? QStringLiteral("auto")
        : documentType.trimmed());
    QNetworkReply *reply = networkManager_.post(
        createRequest(buildProjectReviewStartUrl(), 10000),
        QJsonDocument(payload).toJson(QJsonDocument::Compact));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleProjectReviewStartReply(reply);
    });
}

void BackendClient::requestProjectDocumentReviewResult(const QString &taskId)
{
    if (taskId.trimmed().isEmpty()) {
        emit projectReviewFailed(QStringLiteral("项目审查任务 ID 为空，无法读取已验证报告。"));
        return;
    }
    QNetworkReply *reply = networkManager_.get(
        createRequest(buildProjectReviewResultUrl(taskId), 10000));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleProjectReviewResultReply(reply);
    });
}

void BackendClient::startPaperReview(const QString &documentRef, const QString &paperType)
{
    if (documentRef.trimmed().isEmpty()) {
        emit paperReviewFailed(QStringLiteral("请选择需要审查的论文或学术报告。"));
        return;
    }

    QJsonObject payload;
    payload.insert(QStringLiteral("document_ref"), documentRef.trimmed());
    payload.insert(QStringLiteral("paper_type"), paperType.trimmed().isEmpty()
        ? QStringLiteral("auto")
        : paperType.trimmed());
    QNetworkReply *reply = networkManager_.post(
        createRequest(buildPaperReviewStartUrl(), 10000),
        QJsonDocument(payload).toJson(QJsonDocument::Compact));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handlePaperReviewStartReply(reply);
    });
}

void BackendClient::requestPaperReviewResult(const QString &taskId)
{
    if (taskId.trimmed().isEmpty()) {
        emit paperReviewFailed(QStringLiteral("论文审查任务 ID 为空，无法读取已验证报告。"));
        return;
    }
    QNetworkReply *reply = networkManager_.get(
        createRequest(buildPaperReviewResultUrl(taskId), 10000));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handlePaperReviewResultReply(reply);
    });
}

void BackendClient::requestTaskLogs(const QString &taskId)
{
    if (taskId.isEmpty()) {
        emit taskLogsFailed(QStringLiteral("任务 ID 为空，无法加载日志。"));
        return;
    }

    // 详情日志按需加载，避免历史页一打开就把整页任务日志全部拉回来。
    QNetworkReply *reply = networkManager_.get(createRequest(buildTaskLogsUrl(taskId)));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleTaskLogsReply(reply);
    });
}

void BackendClient::requestTaskPlan(const QString &taskId)
{
    if (taskId.isEmpty()) {
        emit taskPlanFailed(QStringLiteral("任务 ID 为空，无法加载总指挥计划。"));
        return;
    }

    // workflow_plan 是静态复盘数据，只在用户选中任务时读取一次，不进入自动刷新链路。
    QNetworkReply *reply = networkManager_.get(createRequest(buildTaskPlanUrl(taskId)));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleTaskPlanReply(reply);
    });
}

void BackendClient::requestTaskMemoryProposals(const QString &taskId)
{
    if (taskId.trimmed().isEmpty()) {
        emit taskMemoryProposalsFailed(taskId, QStringLiteral("任务 ID 为空，无法读取记忆候选。"));
        return;
    }
    QNetworkReply *reply = networkManager_.get(
        createRequest(buildTaskMemoryProposalsUrl(taskId.trimmed()), 10000));
    connect(reply, &QNetworkReply::finished, this, [this, reply, taskId]() {
        reply->deleteLater();
        if (reply->error() != QNetworkReply::NoError) {
            emit taskMemoryProposalsFailed(taskId, replyErrorMessage(reply));
            return;
        }
        const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
        if (!document.isObject()) {
            emit taskMemoryProposalsFailed(taskId, QStringLiteral("长期记忆候选响应格式无效。"));
            return;
        }
        const QJsonObject payload = document.object();
        TaskMemoryProposalListResult result;
        result.taskId = payload.value(QStringLiteral("task_id")).toString();
        result.note = payload.value(QStringLiteral("note")).toString();
        for (const QJsonValue &value : payload.value(QStringLiteral("items")).toArray()) {
            if (!value.isObject()) {
                continue;
            }
            const TaskMemoryProposalInfo proposal = readTaskMemoryProposalInfo(value.toObject());
            if (!proposal.proposalId.isEmpty()) {
                result.items.append(proposal);
            }
        }
        if (result.taskId.isEmpty()) {
            emit taskMemoryProposalsFailed(taskId, QStringLiteral("长期记忆候选响应缺少任务身份。"));
            return;
        }
        emit taskMemoryProposalsReceived(result);
    });
}

void BackendClient::confirmTaskMemoryProposal(
    const QString &taskId,
    const TaskMemoryProposalInfo &proposal,
    const QString &kind,
    const QString &scope,
    const QString &title,
    const QString &summary,
    const QStringList &tags)
{
    if (taskId.trimmed().isEmpty() || proposal.proposalId.trimmed().isEmpty()) {
        emit taskMemoryProposalConfirmFailed(taskId, QStringLiteral("记忆候选身份不完整，请重新打开。"));
        return;
    }
    QJsonArray tagValues;
    for (const QString &tag : tags) {
        if (!tag.trimmed().isEmpty()) {
            tagValues.append(tag.trimmed());
        }
    }
    QJsonObject payload;
    payload.insert(QStringLiteral("proposal_id"), proposal.proposalId);
    payload.insert(QStringLiteral("kind"), kind.trimmed());
    payload.insert(QStringLiteral("scope"), scope.trimmed());
    payload.insert(QStringLiteral("title"), title.trimmed());
    payload.insert(QStringLiteral("summary"), summary.trimmed());
    payload.insert(QStringLiteral("tags"), tagValues);
    payload.insert(QStringLiteral("user_confirmed"), true);
    QNetworkReply *reply = networkManager_.post(
        createRequest(buildTaskMemoryProposalConfirmUrl(taskId.trimmed()), 10000),
        QJsonDocument(payload).toJson(QJsonDocument::Compact));
    connect(reply, &QNetworkReply::finished, this, [this, reply, taskId]() {
        reply->deleteLater();
        if (reply->error() != QNetworkReply::NoError) {
            emit taskMemoryProposalConfirmFailed(taskId, replyErrorMessage(reply));
            return;
        }
        emit taskMemoryProposalConfirmed(taskId, QStringLiteral("已保存为长期记忆，可在系统设置中查看或删除。"));
    });
}

void BackendClient::requestTaskPlanVersions(const QString &taskId)
{
    if (taskId.trimmed().isEmpty()) {
        emit taskPlanVersionsFailed(QStringLiteral("任务 ID 为空，无法加载计划版本。"));
        return;
    }

    // 版本列表只含元数据；详情仍由按版本请求获取，避免主调度台读取重复步骤 JSON。
    QNetworkReply *reply = networkManager_.get(createRequest(buildTaskPlanVersionsUrl(taskId.trimmed())));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleTaskPlanVersionsReply(reply);
    });
}

void BackendClient::requestTaskPlanVersion(const QString &taskId, int planVersion)
{
    if (taskId.trimmed().isEmpty() || planVersion < 1) {
        emit taskPlanVersionFailed(QStringLiteral("计划版本无效，无法加载详情。"));
        return;
    }

    QNetworkReply *reply = networkManager_.get(
        createRequest(buildTaskPlanVersionUrl(taskId.trimmed(), planVersion)));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleTaskPlanVersionReply(reply);
    });
}

void BackendClient::reviseTaskPlan(
    const QString &taskId,
    const QString &userGoal,
    const QString &changeSummary)
{
    if (taskId.trimmed().isEmpty()) {
        emit taskPlanRevisionFailed(QStringLiteral("任务 ID 为空，无法修改计划。"));
        return;
    }
    if (userGoal.trimmed().size() < 2 || changeSummary.trimmed().size() < 2) {
        emit taskPlanRevisionFailed(QStringLiteral("请填写新的任务目标和本次修改说明。"));
        return;
    }

    // 客户只能提交目标和修改说明。confirmed 明确表达二次确认，步骤、权限、路径均由后端重建。
    QJsonObject payload;
    payload.insert(QStringLiteral("user_goal"), userGoal.trimmed());
    payload.insert(QStringLiteral("change_summary"), changeSummary.trimmed());
    payload.insert(QStringLiteral("confirmed"), true);
    QNetworkReply *reply = networkManager_.post(
        createRequest(buildTaskPlanRevisionUrl(taskId.trimmed()), 45000),
        QJsonDocument(payload).toJson(QJsonDocument::Compact));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleTaskPlanRevisionReply(reply);
    });
}

void BackendClient::requestTaskSteps(const QString &taskId)
{
    if (taskId.isEmpty()) {
        emit taskStepsFailed(QStringLiteral("任务 ID 为空，无法加载步骤。"));
        return;
    }

    // step 级结果只在用户选中任务时按需读取，避免历史列表接口承担详情负载。
    QNetworkReply *reply = networkManager_.get(createRequest(buildTaskStepsUrl(taskId)));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleTaskStepsReply(reply);
    });
}

void BackendClient::requestTaskPermissions(const QString &taskId)
{
    if (taskId.isEmpty()) {
        emit taskPermissionsFailed(QStringLiteral("任务 ID 为空，无法加载权限请求。"));
        return;
    }

    QNetworkReply *reply = networkManager_.get(createRequest(buildTaskPermissionsUrl(taskId)));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleTaskPermissionsReply(reply);
    });
}

void BackendClient::requestTaskRuntimeState(const QString &taskId)
{
    if (taskId.isEmpty()) {
        emit taskRuntimeStateFailed(QStringLiteral("任务 ID 为空，无法加载运行态。"));
        return;
    }

    // 运行态快照很轻，只用于按钮和状态提示，不需要长超时。
    QNetworkReply *reply = networkManager_.get(createRequest(buildTaskRuntimeStateUrl(taskId)));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleTaskRuntimeStateReply(reply);
    });
}

void BackendClient::requestTaskMetrics(const QString &taskId)
{
    if (taskId.isEmpty()) {
        emit taskMetricsFailed(QStringLiteral("任务 ID 为空，无法加载运行指标。"));
        return;
    }

    // metrics 只在用户查看详情时按需读取，避免历史列表接口承担评估数据负载。
    QNetworkReply *reply = networkManager_.get(createRequest(buildTaskMetricsUrl(taskId)));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleTaskMetricsReply(reply);
    });
}

void BackendClient::requestTaskModelRoutes(const QString &taskId)
{
    if (taskId.isEmpty()) {
        emit taskModelRoutesFailed(QStringLiteral("任务 ID 为空，无法加载实际模型审计。"));
        return;
    }

    // 审计快照与任务详情分开按需读取，避免历史列表或常规运行指标承载低频明细。
    QNetworkReply *reply = networkManager_.get(createRequest(buildTaskModelRoutesUrl(taskId)));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleTaskModelRoutesReply(reply);
    });
}

void BackendClient::requestTaskEvaluation(const QString &taskId)
{
    if (taskId.isEmpty()) {
        emit taskEvaluationFailed(QStringLiteral("任务 ID 为空，无法加载任务评估。"));
        return;
    }

    // evaluation 是 metrics 的用户解释层，按详情页选中任务读取，避免历史列表变重。
    QNetworkReply *reply = networkManager_.get(createRequest(buildTaskEvaluationUrl(taskId)));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleTaskEvaluationReply(reply);
    });
}

void BackendClient::requestNodeContracts()
{
    // Node Contract 是低频静态协议，启动后拉一次即可；后续如支持热加载再加显式刷新。
    QNetworkReply *reply = networkManager_.get(createRequest(buildNodeContractsUrl()));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleNodeContractsReply(reply);
    });
}

void BackendClient::checkWorkflowCommandPolicy(const QString &command, const QString &cwd)
{
    const QString trimmedCommand = command.trimmed();
    if (trimmedCommand.isEmpty()) {
        emit workflowCommandPolicyCheckFailed(QStringLiteral("请输入要检查的命令。"));
        return;
    }

    QJsonObject payload;
    payload.insert(QStringLiteral("command"), trimmedCommand);
    if (!cwd.trimmed().isEmpty()) {
        payload.insert(QStringLiteral("cwd"), cwd.trimmed());
    }

    // 该接口只做静态分类，不执行命令；超时给 10 秒，避免后端短暂忙碌时误伤 UI 体验。
    QNetworkReply *reply = networkManager_.post(
        createRequest(buildWorkflowCommandPolicyUrl(), 10000),
        QJsonDocument(payload).toJson(QJsonDocument::Compact));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleWorkflowCommandPolicyReply(reply);
    });
}

void BackendClient::requestRuntimePreferences()
{
    QNetworkReply *reply = networkManager_.get(createRequest(buildRuntimePreferencesUrl()));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleRuntimePreferencesReply(reply);
    });
}

void BackendClient::saveRuntimePreferences(
    const QString &permissionPolicy,
    const QString &personality,
    bool memoryEnabled)
{
    if (permissionPolicy.trimmed().isEmpty() || personality.trimmed().isEmpty()) {
        emit runtimePreferencesSaveFailed(QStringLiteral("权限模式或 Agent 风格为空。"));
        return;
    }

    QJsonObject payload;
    payload.insert(QStringLiteral("permission_policy"), permissionPolicy.trimmed());
    payload.insert(QStringLiteral("personality"), personality.trimmed());
    // 开关只表达“是否允许读取已确认的短记忆”；记忆内容不会经由设置接口传输。
    payload.insert(QStringLiteral("memory_enabled"), memoryEnabled);

    QNetworkReply *reply = networkManager_.put(
        createRequest(buildRuntimePreferencesUrl(), 10000),
        QJsonDocument(payload).toJson(QJsonDocument::Compact));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleRuntimePreferencesSaveReply(reply);
    });
}

void BackendClient::requestLongTermMemories(const QString &scope)
{
    QNetworkReply *reply = networkManager_.get(createRequest(buildLongTermMemoriesUrl(scope)));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleLongTermMemoriesReply(reply);
    });
}

void BackendClient::createLongTermMemory(
    const QString &kind,
    const QString &scope,
    const QString &title,
    const QString &summary,
    const QStringList &tags)
{
    QJsonObject payload;
    payload.insert(QStringLiteral("kind"), kind.trimmed());
    payload.insert(QStringLiteral("scope"), scope.trimmed());
    payload.insert(QStringLiteral("title"), title.trimmed());
    payload.insert(QStringLiteral("summary"), summary.trimmed());
    payload.insert(QStringLiteral("user_confirmed"), true);
    QJsonArray tagValues;
    for (const QString &tag : tags) {
        if (!tag.trimmed().isEmpty()) {
            tagValues.append(tag.trimmed());
        }
    }
    payload.insert(QStringLiteral("tags"), tagValues);

    QNetworkReply *reply = networkManager_.post(
        createRequest(buildLongTermMemoriesUrl(), 10000),
        QJsonDocument(payload).toJson(QJsonDocument::Compact));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleLongTermMemoryMutationReply(reply, QStringLiteral("长期记忆已保存。"));
    });
}

void BackendClient::updateLongTermMemory(
    const QString &memoryId,
    const QString &title,
    const QString &summary,
    const QStringList &tags,
    bool enabled)
{
    if (memoryId.trimmed().isEmpty()) {
        emit longTermMemoryMutationFailed(QStringLiteral("未选择需要更新的长期记忆。"));
        return;
    }
    QJsonObject payload;
    payload.insert(QStringLiteral("title"), title.trimmed());
    payload.insert(QStringLiteral("summary"), summary.trimmed());
    payload.insert(QStringLiteral("enabled"), enabled);
    QJsonArray tagValues;
    for (const QString &tag : tags) {
        if (!tag.trimmed().isEmpty()) {
            tagValues.append(tag.trimmed());
        }
    }
    payload.insert(QStringLiteral("tags"), tagValues);

    QUrl url(buildLongTermMemoriesUrl());
    url.setPath(
        QStringLiteral("/api/memories/%1")
            .arg(QString::fromLatin1(QUrl::toPercentEncoding(memoryId.trimmed()))));
    QNetworkReply *reply = networkManager_.put(
        createRequest(url, 10000),
        QJsonDocument(payload).toJson(QJsonDocument::Compact));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleLongTermMemoryMutationReply(reply, QStringLiteral("长期记忆已更新。"));
    });
}

void BackendClient::deleteLongTermMemory(const QString &memoryId)
{
    if (memoryId.trimmed().isEmpty()) {
        emit longTermMemoryMutationFailed(QStringLiteral("未选择需要删除的长期记忆。"));
        return;
    }
    QUrl url(buildLongTermMemoriesUrl());
    url.setPath(
        QStringLiteral("/api/memories/%1")
            .arg(QString::fromLatin1(QUrl::toPercentEncoding(memoryId.trimmed()))));
    QNetworkReply *reply = networkManager_.sendCustomRequest(createRequest(url, 10000), "DELETE");
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleLongTermMemoryMutationReply(reply, QStringLiteral("长期记忆已删除。"));
    });
}

void BackendClient::clearLongTermMemories(const QString &scope)
{
    QNetworkReply *reply = networkManager_.sendCustomRequest(
        createRequest(buildLongTermMemoriesUrl(scope, true), 10000),
        "DELETE");
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleLongTermMemoryMutationReply(reply, QStringLiteral("当前范围的长期记忆已清空。"));
    });
}

void BackendClient::requestTaskArtifacts(const QString &taskId)
{
    if (taskId.isEmpty()) {
        emit taskArtifactsFailed(QStringLiteral("任务 ID 为空，无法加载产物。"));
        return;
    }

    QNetworkReply *reply = networkManager_.get(createRequest(buildTaskArtifactsUrl(taskId)));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleTaskArtifactsReply(reply);
    });
}

void BackendClient::requestTaskArtifactPreview(const QString &taskId, const QString &artifactId, int maxBytes)
{
    if (taskId.isEmpty() || artifactId.isEmpty()) {
        emit taskArtifactPreviewFailed(taskId, artifactId, QStringLiteral("任务 ID 或产物 ID 为空，无法预览产物。"));
        return;
    }

    // 预览接口由后端做安全边界；这里给稍长超时，避免大一点的文本产物在本机 I/O 忙时误报失败。
    QNetworkReply *reply = networkManager_.get(
        createRequest(buildTaskArtifactPreviewUrl(taskId, artifactId, maxBytes), 10000));
    connect(reply, &QNetworkReply::finished, this, [this, reply, taskId, artifactId]() {
        handleTaskArtifactPreviewReply(reply, taskId, artifactId);
    });
}

void BackendClient::requestTaskArtifactOpen(const QString &taskId, const QString &artifactId)
{
    if (taskId.isEmpty() || artifactId.isEmpty()) {
        emit taskArtifactOpenFailed(taskId, artifactId, QStringLiteral("任务 ID 或产物 ID 为空，无法打开产物。"));
        return;
    }

    // 绝对路径只保存在后端 artifact metadata 中。Qt 只提交任务和产物标识，目录约束由 API 复核。
    QNetworkReply *reply = networkManager_.post(
        createRequest(buildTaskArtifactOpenUrl(taskId, artifactId), 10000), QByteArrayLiteral("{}"));
    connect(reply, &QNetworkReply::finished, this, [this, reply, taskId, artifactId]() {
        handleTaskArtifactOpenReply(reply, taskId, artifactId);
    });
}

void BackendClient::requestTaskToolCalls(const QString &taskId)
{
    if (taskId.isEmpty()) {
        emit taskToolCallsFailed(QStringLiteral("任务 ID 为空，无法加载工具调用。"));
        return;
    }

    QNetworkReply *reply = networkManager_.get(createRequest(buildTaskToolCallsUrl(taskId)));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleTaskToolCallsReply(reply);
    });
}

void BackendClient::requestTaskUpdates(const QString &taskId)
{
    if (taskId.isEmpty()) {
        emit taskUpdatesFailed(QString(), QStringLiteral("任务 ID 为空，无法加载事件流。"));
        return;
    }

    // updates 是后端聚合后的时间线，比前端同时拼 logs/steps/artifacts 更稳定。
    QNetworkReply *reply = networkManager_.get(createRequest(buildTaskUpdatesUrl(taskId)));
    connect(reply, &QNetworkReply::finished, this, [this, reply, taskId]() {
        handleTaskUpdatesReply(reply, taskId);
    });
}

void BackendClient::requestTaskDeliveryCard(const QString &taskId)
{
    const QString normalizedTaskId = taskId.trimmed();
    if (normalizedTaskId.isEmpty()) {
        emit taskDeliveryCardFailed(taskId, QStringLiteral("任务 ID 为空，无法加载交付结果。"));
        return;
    }

    // 交付卡是轻量 JSON，使用独立请求不会阻塞已有 WebSocket 日志和历史详情请求。
    QNetworkReply *reply = networkManager_.get(
        createRequest(buildTaskDeliveryCardUrl(normalizedTaskId), 10000));
    connect(reply, &QNetworkReply::finished, this, [this, reply, normalizedTaskId]() {
        handleTaskDeliveryCardReply(reply, normalizedTaskId);
    });
}

void BackendClient::requestTaskPermissionDecision(
    const QString &taskId,
    const QString &requestId,
    const QString &decision,
    const QString &decidedBy,
    const QString &note)
{
    if (taskId.isEmpty() || requestId.isEmpty()) {
        emit taskPermissionDecisionFailed(QStringLiteral("任务 ID 或权限请求 ID 为空。"));
        return;
    }

    QJsonObject payload;
    payload.insert(QStringLiteral("decision"), decision);
    payload.insert(QStringLiteral("decided_by"), decidedBy);
    payload.insert(QStringLiteral("note"), note);

    QNetworkReply *reply = networkManager_.post(
        createRequest(buildTaskPermissionDecisionUrl(taskId, requestId)),
        QJsonDocument(payload).toJson(QJsonDocument::Compact));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleTaskPermissionDecisionReply(reply);
    });
}

void BackendClient::requestTaskCancel(const QString &taskId)
{
    if (taskId.isEmpty()) {
        emit taskControlFailed(QStringLiteral("任务 ID 为空，无法请求取消。"));
        return;
    }

    // dry-run 阶段取消通常会返回 accepted=false，但仍通过统一控制接口走一遍，
    // 这样前端控制按钮可以提前对齐真实执行器的协议形状。
    QNetworkReply *reply = networkManager_.post(createRequest(buildTaskControlUrl(taskId, QStringLiteral("cancel"))), QByteArray());
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleTaskControlReply(reply);
    });
}

void BackendClient::requestTaskPause(const QString &taskId)
{
    if (taskId.isEmpty()) {
        emit taskControlFailed(QStringLiteral("任务 ID 为空，无法请求暂停。"));
        return;
    }

    QNetworkReply *reply = networkManager_.post(
        createRequest(buildTaskControlUrl(taskId, QStringLiteral("pause"))), QByteArray());
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleTaskControlReply(reply);
    });
}

void BackendClient::requestTaskRetry(const QString &taskId)
{
    if (taskId.isEmpty()) {
        emit taskControlFailed(QStringLiteral("任务 ID 为空，无法请求重试。"));
        return;
    }

    // retry 不重新调用模型，只复用后端缓存的 workflow_plan；这里主要关注新生成的 task_id。
    QNetworkReply *reply = networkManager_.post(createRequest(buildTaskControlUrl(taskId, QStringLiteral("retry"))), QByteArray());
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleTaskControlReply(reply);
    });
}

void BackendClient::requestTaskExecute(const QString &taskId)
{
    if (taskId.isEmpty()) {
        emit taskExecutionFailed(QStringLiteral("任务 ID 为空，无法请求执行。"));
        return;
    }

    // C3 起 /start 只负责受理和返回 runtime_task_id；模型/Tool 在后端 Job 中执行，Qt 线程
    // 不必等待长任务结束。实际进度继续通过 WebSocket、updates 和历史详情显示。
    QNetworkReply *reply = networkManager_.post(createRequest(buildTaskExecuteUrl(taskId), 10000), QByteArray());
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        handleTaskExecutionReply(reply);
    });
}

void BackendClient::connectTaskLog(const QString &taskId)
{
    if (taskId.isEmpty()) {
        return;
    }

    if (taskSocket_.state() != QAbstractSocket::UnconnectedState) {
        // 新任务开始时中断旧日志流，避免旧任务的尾部日志写进新任务 UI。
        activeLogTaskId_.clear();
        taskSocket_.abort();
    }

    QUrl url(baseUrl_);
    url.setScheme(QStringLiteral("ws"));
    url.setPath(QStringLiteral("/ws/tasks/%1").arg(taskId));

    activeLogTaskId_ = taskId;
    taskSocketHadError_ = false;
    taskSocket_.open(url);
}

QUrl BackendClient::baseUrl() const
{
    return baseUrl_;
}

QNetworkRequest BackendClient::createRequest(const QString &path, int transferTimeoutMs) const
{
    QUrl url(baseUrl_);
    url.setPath(path);
    return createRequest(url, transferTimeoutMs);
}

QNetworkRequest BackendClient::createRequest(const QUrl &url, int transferTimeoutMs) const
{
    QNetworkRequest request(url);

    // 显式声明 UTF-8，避免中文任务在不同系统区域设置下出现乱码。
    request.setHeader(QNetworkRequest::ContentTypeHeader, QStringLiteral("application/json; charset=utf-8"));
    request.setTransferTimeout(transferTimeoutMs);
    return request;
}

void BackendClient::requestHealth()
{
    // 健康检查成功后再加载 Agent 列表，避免后端离线时并发打多条失败请求。
    QNetworkReply *reply = networkManager_.get(createRequest(QStringLiteral("/health")));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        reply->deleteLater();

        if (reply->error() != QNetworkReply::NoError) {
            emit healthChecked(false, replyErrorMessage(reply));
            return;
        }

        const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
        const QJsonObject payload = document.object();
        const QString status = payload.value(QStringLiteral("status")).toString();
        if (status != QStringLiteral("ok")) {
            emit healthChecked(false, QStringLiteral("后端健康检查返回异常状态。"));
            return;
        }

        const QString service = payload.value(QStringLiteral("service")).toString(QStringLiteral("AgentFlow Backend"));
        const QString version = payload.value(QStringLiteral("version")).toString(QStringLiteral("unknown"));
        const QString environment = payload.value(QStringLiteral("environment")).toString(QStringLiteral("unknown"));
        QString healthMessage = QStringLiteral("%1 %2 · %3").arg(service, version, environment);

        // 可选工作台的依赖缺失不应该伪装成“后端离线”。这里保留全局可用状态，同时让客户
        // 在刚启动时就知道数据画像暂不可用，而不是导入自己的文件后才收到模糊的 HTTP 400。
        const QJsonObject capabilities = payload.value(QStringLiteral("capabilities")).toObject();
        const QJsonObject dataWorkspace = capabilities.value(QStringLiteral("data_workspace")).toObject();
        if (!dataWorkspace.isEmpty() && !dataWorkspace.value(QStringLiteral("ready")).toBool(true)) {
            const QString detail = dataWorkspace.value(QStringLiteral("message")).toString();
            healthMessage += QStringLiteral(" · %1").arg(
                detail.isEmpty() ? QStringLiteral("数据工作台暂不可用") : detail);
        }

        emit healthChecked(true, healthMessage);

        requestAgents();
    });
}

void BackendClient::requestAgents()
{
    QNetworkReply *reply = networkManager_.get(createRequest(QStringLiteral("/api/agents")));
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        reply->deleteLater();

        if (reply->error() != QNetworkReply::NoError) {
            emit agentsLoadFailed(replyErrorMessage(reply));
            return;
        }

        const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
        const QJsonArray agentsArray = document.object().value(QStringLiteral("agents")).toArray();
        QList<AgentInfo> agents;
        agents.reserve(agentsArray.size());

        for (const QJsonValue &value : agentsArray) {
            // 只在客户端保存展示所需字段；插件权限等复杂配置后续由专门页面读取。
            const QJsonObject object = value.toObject();
            AgentInfo agent;
            agent.id = object.value(QStringLiteral("id")).toString();
            agent.name = object.value(QStringLiteral("name")).toString();
            agent.description = object.value(QStringLiteral("description")).toString();
            agent.category = object.value(QStringLiteral("category")).toString();
            agent.version = object.value(QStringLiteral("version")).toString();
            agent.enabled = object.value(QStringLiteral("enabled")).toBool();
            agent.builtin = object.value(QStringLiteral("builtin")).toBool();
            agent.capabilities = readStringList(object.value(QStringLiteral("capabilities")).toArray());
            agents.append(agent);
        }

        emit agentsLoaded(agents);
    });
}

QUrl BackendClient::buildTaskHistoryUrl(const TaskHistoryQuery &query) const
{
    // 这里把筛选条件转成 query string，和后端 TaskHistoryQuery 保持一一对应。
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/tasks"));
    QUrlQuery urlQuery;
    urlQuery.addQueryItem(QStringLiteral("limit"), QString::number(query.limit));
    urlQuery.addQueryItem(QStringLiteral("offset"), QString::number(query.offset));

    if (!query.status.isEmpty()) {
        urlQuery.addQueryItem(QStringLiteral("status"), query.status);
    }
    if (!query.mode.isEmpty()) {
        urlQuery.addQueryItem(QStringLiteral("mode"), query.mode);
    }
    if (!query.maxRiskLevel.isEmpty()) {
        urlQuery.addQueryItem(QStringLiteral("max_risk_level"), query.maxRiskLevel);
    }
    if (query.requiresConfirmation >= 0) {
        urlQuery.addQueryItem(QStringLiteral("requires_confirmation"),
                              query.requiresConfirmation > 0 ? QStringLiteral("true")
                                                            : QStringLiteral("false"));
    }

    url.setQuery(urlQuery);
    return url;
}

QUrl BackendClient::buildModelProvidersUrl() const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/models/providers"));
    return url;
}

QUrl BackendClient::buildModelRoutesUrl() const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/models/routes"));
    return url;
}

QUrl BackendClient::buildModelRouteUrl(const QString &routeId) const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/models/routes/%1").arg(routeId));
    return url;
}

QUrl BackendClient::buildModelConfigUrl() const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/models/config"));
    return url;
}

QUrl BackendClient::buildModelTestUrl() const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/models/test"));
    return url;
}

QUrl BackendClient::buildWorkspaceDocumentsUrl() const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/workspace/documents"));
    return url;
}

QUrl BackendClient::buildKnowledgeBasesUrl() const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/knowledge/bases"));
    return url;
}

QUrl BackendClient::buildKnowledgeDocumentsUrl() const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/knowledge/documents/import"));
    return url;
}

QUrl BackendClient::buildKnowledgeBaseDocumentsUrl(const QString &knowledgeBaseId) const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/knowledge/bases/%1/documents").arg(knowledgeBaseId));
    return url;
}

QUrl BackendClient::buildKnowledgeIndexStartUrl(const QString &knowledgeBaseId) const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/knowledge/bases/%1/index/start").arg(knowledgeBaseId));
    return url;
}

QUrl BackendClient::buildKnowledgeIndexJobUrl(const QString &indexJobId) const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/knowledge/index-jobs/%1").arg(indexJobId));
    return url;
}

QUrl BackendClient::buildKnowledgeVectorCapabilityUrl() const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/knowledge/vector-capability"));
    return url;
}

QUrl BackendClient::buildKnowledgeVectorPrepareUrl() const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/knowledge/vector-model/prepare"));
    return url;
}

QUrl BackendClient::buildKnowledgeOcrCapabilityUrl() const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/knowledge/ocr-capability"));
    return url;
}

QUrl BackendClient::buildKnowledgeOcrPrepareUrl() const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/knowledge/ocr-model/prepare"));
    return url;
}

QUrl BackendClient::buildKnowledgeOcrPreparationUrl(const QString &preparationId) const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/knowledge/ocr-preparations/%1").arg(preparationId));
    return url;
}

QUrl BackendClient::buildKnowledgeBaseUrl(const QString &knowledgeBaseId) const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/knowledge/bases/%1").arg(knowledgeBaseId));
    return url;
}

QUrl BackendClient::buildKnowledgeAnswerStartUrl() const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/knowledge/answer/start"));
    return url;
}

QUrl BackendClient::buildKnowledgeAnswerResultUrl(const QString &taskId) const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/knowledge/answers/%1/result").arg(taskId));
    return url;
}

QUrl BackendClient::buildKnowledgeDeepTaskStartUrl() const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/knowledge/deep-tasks/start"));
    return url;
}

QUrl BackendClient::buildKnowledgeDeepTaskResultUrl(const QString &taskId) const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/knowledge/deep-tasks/%1/result").arg(taskId));
    return url;
}

QUrl BackendClient::buildKnowledgeDeepTaskControlUrl(const QString &taskId, const QString &action) const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/knowledge/deep-tasks/%1/%2").arg(taskId, action));
    return url;
}

QUrl BackendClient::buildKnowledgeDeepTaskReportUrl(const QString &taskId) const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/knowledge/deep-tasks/%1/report").arg(taskId));
    return url;
}

QUrl BackendClient::buildDataAgentDatasetsUrl() const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/agents/data_agent/datasets"));
    return url;
}

QUrl BackendClient::buildDataAgentDatasetProfileUrl(const QString &datasetName) const
{
    QUrl url(baseUrl_);
    // setPath 的默认 DecodedMode 接收自然 QString，并由 QUrl 在发送 HTTP 前完成一次编码。
    // 先手工 toPercentEncoding 再 setPath 会把 '%' 二次转义，中文、空格或括号文件名最终无法
    // 与后端工作区中的真实文件名匹配。
    url.setPath(QStringLiteral("/api/agents/data_agent/datasets/%1/profile").arg(datasetName));
    return url;
}

QUrl BackendClient::buildDataAgentRecommendationsUrl() const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/agents/data_agent/recommendations"));
    return url;
}

QUrl BackendClient::buildDataAgentAnalysisPreviewUrl() const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/agents/data_agent/analysis/preview"));
    return url;
}

QUrl BackendClient::buildDataAgentAnalysisExportStartUrl() const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/agents/data_agent/analysis/export/start"));
    return url;
}

QUrl BackendClient::buildDataAgentAnalysisExportResultUrl(const QString &taskId) const
{
    QUrl url(baseUrl_);
    const QString encoded = QString::fromUtf8(QUrl::toPercentEncoding(taskId));
    url.setPath(QStringLiteral("/api/agents/data_agent/analysis/export/%1/result").arg(encoded));
    return url;
}

QUrl BackendClient::buildDataAgentChartExportStartUrl() const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/agents/data_agent/charts/export/start"));
    return url;
}

QUrl BackendClient::buildDataAgentChartExportResultUrl(const QString &taskId) const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/agents/data_agent/charts/export/%1/result")
                    .arg(QString::fromUtf8(QUrl::toPercentEncoding(taskId))));
    return url;
}

QUrl BackendClient::buildDataAgentChartImageUrl(const QString &taskId, const QString &artifactId) const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/agents/data_agent/charts/export/%1/artifacts/%2/image")
                    .arg(QString::fromUtf8(QUrl::toPercentEncoding(taskId)),
                         QString::fromUtf8(QUrl::toPercentEncoding(artifactId))));
    return url;
}

QUrl BackendClient::buildDataAgentTransformationPreviewUrl() const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/agents/data_agent/transformations/preview"));
    return url;
}

QUrl BackendClient::buildDataAgentTransformationExportStartUrl() const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/agents/data_agent/transformations/export/start"));
    return url;
}

QUrl BackendClient::buildDataAgentTransformationExportResultUrl(const QString &taskId) const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/agents/data_agent/transformations/export/%1/result")
                    .arg(QString::fromUtf8(QUrl::toPercentEncoding(taskId))));
    return url;
}

QUrl BackendClient::buildDocumentAgentStartUrl() const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/agents/document_agent/start"));
    return url;
}

QUrl BackendClient::buildDocumentAgentResultUrl(const QString &taskId) const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/agents/document_agent/%1/result").arg(taskId));
    return url;
}

QUrl BackendClient::buildPdfProcessingStartUrl() const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/agents/document_agent/pdf-tools/start"));
    return url;
}

QUrl BackendClient::buildPdfProcessingResultUrl(const QString &taskId) const
{
    QUrl url(baseUrl_);
    url.setPath(
        QStringLiteral("/api/agents/document_agent/pdf-tools/%1/result")
            .arg(QString::fromUtf8(QUrl::toPercentEncoding(taskId))));
    return url;
}

QUrl BackendClient::buildDocumentDraftSectionStartUrl(const QString &sourceTaskId) const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/agents/document_agent/%1/draft-sections/start")
                    .arg(QString::fromUtf8(QUrl::toPercentEncoding(sourceTaskId))));
    return url;
}

QUrl BackendClient::buildDocumentDraftReviewStartUrl(const QString &sourceTaskId) const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/agents/document_agent/%1/draft-review/start")
                    .arg(QString::fromUtf8(QUrl::toPercentEncoding(sourceTaskId))));
    return url;
}

QUrl BackendClient::buildDocumentDraftSectionReviewStartUrl(const QString &sourceTaskId) const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/agents/document_agent/%1/draft-sections/review/start")
                    .arg(QString::fromUtf8(QUrl::toPercentEncoding(sourceTaskId))));
    return url;
}

QUrl BackendClient::buildDocumentDraftSectionRevisionPreviewStartUrl(const QString &sourceReviewTaskId) const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/agents/document_agent/%1/draft-sections/revision-preview/start")
                    .arg(QString::fromUtf8(QUrl::toPercentEncoding(sourceReviewTaskId))));
    return url;
}

QUrl BackendClient::buildDocumentDraftSectionBatchRevisionPreviewStartUrl(const QString &sourceReviewTaskId) const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/agents/document_agent/%1/draft-sections/revision-batch-preview/start")
                    .arg(QString::fromUtf8(QUrl::toPercentEncoding(sourceReviewTaskId))));
    return url;
}

QUrl BackendClient::buildDocumentDraftSectionManualRevisionPreviewStartUrl(const QString &sourceTaskId) const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/agents/document_agent/%1/draft-sections/manual-revision-preview/start")
                    .arg(QString::fromUtf8(QUrl::toPercentEncoding(sourceTaskId))));
    return url;
}

QUrl BackendClient::buildDocumentDraftRestorePreviewStartUrl(const QString &sourceTaskId) const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/agents/document_agent/%1/restore-preview/start")
                    .arg(QString::fromUtf8(QUrl::toPercentEncoding(sourceTaskId))));
    return url;
}

QUrl BackendClient::buildDocumentDraftTemplatePreviewStartUrl(const QString &sourceTaskId) const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/agents/document_agent/%1/template-preview/start")
                    .arg(QString::fromUtf8(QUrl::toPercentEncoding(sourceTaskId))));
    return url;
}

QUrl BackendClient::buildDocumentDraftMergeCandidatesUrl(const QString &taskId) const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/agents/document_agent/%1/merge-candidates")
                    .arg(QString::fromUtf8(QUrl::toPercentEncoding(taskId))));
    return url;
}

QUrl BackendClient::buildDocumentDraftMergePlanUrl(
    const QString &primaryTaskId,
    const QString &secondaryTaskId) const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/agents/document_agent/%1/merge-plan/%2")
                    .arg(QString::fromUtf8(QUrl::toPercentEncoding(primaryTaskId)),
                         QString::fromUtf8(QUrl::toPercentEncoding(secondaryTaskId))));
    return url;
}

QUrl BackendClient::buildDocumentDraftMergePreviewStartUrl(const QString &primaryTaskId) const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/agents/document_agent/%1/merge-preview/start")
                    .arg(QString::fromUtf8(QUrl::toPercentEncoding(primaryTaskId))));
    return url;
}

QUrl BackendClient::buildDocumentDraftParentDiffUrl(const QString &taskId) const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/agents/document_agent/%1/version-diff")
                    .arg(QString::fromUtf8(QUrl::toPercentEncoding(taskId))));
    return url;
}

QUrl BackendClient::buildDocumentDraftSaveUrl(const QString &taskId) const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/agents/document_agent/%1/save-draft")
                    .arg(QString::fromUtf8(QUrl::toPercentEncoding(taskId))));
    return url;
}

QUrl BackendClient::buildPresentationPreviewUrl(const QString &taskId) const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/agents/document_agent/%1/presentation-preview")
                    .arg(QString::fromUtf8(QUrl::toPercentEncoding(taskId))));
    return url;
}

QUrl BackendClient::buildPresentationExportUrl(const QString &taskId) const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/agents/document_agent/%1/presentations/export")
                    .arg(QString::fromUtf8(QUrl::toPercentEncoding(taskId))));
    return url;
}

QUrl BackendClient::buildPresentationStudioStartUrl() const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/agents/document_agent/presentation-studio/start"));
    return url;
}

QUrl BackendClient::buildPresentationStudioResultUrl(const QString &taskId) const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/agents/document_agent/presentation-studio/%1/result")
                    .arg(QString::fromUtf8(QUrl::toPercentEncoding(taskId))));
    return url;
}

QUrl BackendClient::buildPresentationStudioExportUrl(const QString &taskId) const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/agents/document_agent/presentation-studio/%1/export")
                    .arg(QString::fromUtf8(QUrl::toPercentEncoding(taskId))));
    return url;
}

QUrl BackendClient::buildPresentationStudioExportPrepareUrl(const QString &taskId) const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/agents/document_agent/presentation-studio/%1/export/prepare")
                    .arg(QString::fromUtf8(QUrl::toPercentEncoding(taskId))));
    return url;
}

QUrl BackendClient::buildProjectReviewRunUrl() const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/agents/document_agent/project-review/run"));
    return url;
}

QUrl BackendClient::buildProjectReviewStartUrl() const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/agents/document_agent/project-review/start"));
    return url;
}

QUrl BackendClient::buildProjectReviewResultUrl(const QString &taskId) const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/agents/document_agent/project-review/%1/result")
                    .arg(QString::fromUtf8(QUrl::toPercentEncoding(taskId))));
    return url;
}

QUrl BackendClient::buildPaperReviewRunUrl() const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/agents/document_agent/paper-review/run"));
    return url;
}

QUrl BackendClient::buildPaperReviewStartUrl() const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/agents/document_agent/paper-review/start"));
    return url;
}

QUrl BackendClient::buildPaperReviewResultUrl(const QString &taskId) const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/agents/document_agent/paper-review/%1/result")
                    .arg(QString::fromUtf8(QUrl::toPercentEncoding(taskId))));
    return url;
}

QUrl BackendClient::buildTaskLogsUrl(const QString &taskId) const
{
    // task_id 来自后端列表或 chat 返回值，仍使用 URL 编码，避免未来 ID 格式扩展时破坏路径。
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/tasks/%1/logs").arg(QString::fromUtf8(QUrl::toPercentEncoding(taskId))));
    return url;
}

QUrl BackendClient::buildTaskPlanUrl(const QString &taskId) const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/tasks/%1/plan").arg(QString::fromUtf8(QUrl::toPercentEncoding(taskId))));
    return url;
}

QUrl BackendClient::buildTaskMemoryProposalsUrl(const QString &taskId) const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/tasks/%1/memory-proposals")
                    .arg(QString::fromUtf8(QUrl::toPercentEncoding(taskId))));
    return url;
}

QUrl BackendClient::buildTaskMemoryProposalConfirmUrl(const QString &taskId) const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/tasks/%1/memory-proposals/confirm")
                    .arg(QString::fromUtf8(QUrl::toPercentEncoding(taskId))));
    return url;
}

QUrl BackendClient::buildTaskPlanVersionsUrl(const QString &taskId) const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/tasks/%1/plan-versions")
                    .arg(QString::fromUtf8(QUrl::toPercentEncoding(taskId))));
    return url;
}

QUrl BackendClient::buildTaskPlanVersionUrl(const QString &taskId, int planVersion) const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/tasks/%1/plan-versions/%2")
                    .arg(QString::fromUtf8(QUrl::toPercentEncoding(taskId)))
                    .arg(planVersion));
    return url;
}

QUrl BackendClient::buildTaskPlanRevisionUrl(const QString &taskId) const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/tasks/%1/plan-revisions")
                    .arg(QString::fromUtf8(QUrl::toPercentEncoding(taskId))));
    return url;
}

QUrl BackendClient::buildTaskStepsUrl(const QString &taskId) const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/tasks/%1/steps").arg(QString::fromUtf8(QUrl::toPercentEncoding(taskId))));
    return url;
}

QUrl BackendClient::buildTaskPermissionsUrl(const QString &taskId) const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/tasks/%1/permissions").arg(QString::fromUtf8(QUrl::toPercentEncoding(taskId))));
    return url;
}

QUrl BackendClient::buildTaskRuntimeStateUrl(const QString &taskId) const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/tasks/%1/runtime-state").arg(QString::fromUtf8(QUrl::toPercentEncoding(taskId))));
    return url;
}

QUrl BackendClient::buildTaskMetricsUrl(const QString &taskId) const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/tasks/%1/metrics").arg(QString::fromUtf8(QUrl::toPercentEncoding(taskId))));
    return url;
}

QUrl BackendClient::buildTaskModelRoutesUrl(const QString &taskId) const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/tasks/%1/model-routes").arg(QString::fromUtf8(QUrl::toPercentEncoding(taskId))));
    return url;
}

QUrl BackendClient::buildTaskEvaluationUrl(const QString &taskId) const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/tasks/%1/evaluation").arg(QString::fromUtf8(QUrl::toPercentEncoding(taskId))));
    return url;
}

QUrl BackendClient::buildNodeContractsUrl() const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/workflow/node-contracts"));
    return url;
}

QUrl BackendClient::buildWorkflowCommandPolicyUrl() const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/workflow/command-policy/check"));
    return url;
}

QUrl BackendClient::buildRuntimePreferencesUrl() const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/settings/runtime-preferences"));
    return url;
}

QUrl BackendClient::buildMcpConnectionsUrl() const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/mcp/connections"));
    return url;
}

QUrl BackendClient::buildPublicReferenceMcpActionUrl(const QString &action) const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/mcp/connections/public-reference/%1").arg(action));
    return url;
}

QUrl BackendClient::buildLongTermMemoriesUrl(const QString &scope, bool confirm) const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/memories"));
    QUrlQuery query;
    if (!scope.trimmed().isEmpty()) {
        query.addQueryItem(QStringLiteral("scope"), scope.trimmed());
    }
    if (confirm) {
        query.addQueryItem(QStringLiteral("confirm"), QStringLiteral("true"));
    }
    url.setQuery(query);
    return url;
}

QUrl BackendClient::buildTaskArtifactsUrl(const QString &taskId) const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/tasks/%1/artifacts").arg(QString::fromUtf8(QUrl::toPercentEncoding(taskId))));
    return url;
}

QUrl BackendClient::buildTaskArtifactPreviewUrl(const QString &taskId, const QString &artifactId, int maxBytes) const
{
    QUrl url(baseUrl_);
    // setPath 的默认 DecodedMode 会负责一次正确的 URL 编码。这里不能预先 percent-encode，
    // 否则 artifact ID 中的 ':' 会变成字面量 "%3A" 传给 FastAPI，历史产物无法精确匹配。
    url.setPath(QStringLiteral("/api/tasks/%1/artifacts/%2/preview")
                    .arg(taskId, artifactId));

    QUrlQuery query;
    query.addQueryItem(QStringLiteral("max_bytes"), QString::number(maxBytes));
    url.setQuery(query);
    return url;
}

QUrl BackendClient::buildTaskArtifactOpenUrl(const QString &taskId, const QString &artifactId) const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/tasks/%1/artifacts/%2/open")
                    .arg(taskId, artifactId));
    return url;
}

QUrl BackendClient::buildTaskToolCallsUrl(const QString &taskId) const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/tasks/%1/tool-calls").arg(QString::fromUtf8(QUrl::toPercentEncoding(taskId))));
    return url;
}

QUrl BackendClient::buildTaskUpdatesUrl(const QString &taskId) const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/tasks/%1/updates").arg(QString::fromUtf8(QUrl::toPercentEncoding(taskId))));
    return url;
}

QUrl BackendClient::buildTaskDeliveryCardUrl(const QString &taskId) const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/tasks/%1/delivery")
                   .arg(QString::fromUtf8(QUrl::toPercentEncoding(taskId))));
    return url;
}

QUrl BackendClient::buildTaskPermissionDecisionUrl(const QString &taskId, const QString &requestId) const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/tasks/%1/permissions/%2/decision")
                    .arg(QString::fromUtf8(QUrl::toPercentEncoding(taskId)),
                         QString::fromUtf8(QUrl::toPercentEncoding(requestId))));
    return url;
}

QUrl BackendClient::buildTaskControlUrl(const QString &taskId, const QString &action) const
{
    // action 只由 requestTaskCancel/requestTaskRetry 传入固定值，task_id 仍做 URL 编码。
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/tasks/%1/%2")
                    .arg(QString::fromUtf8(QUrl::toPercentEncoding(taskId)), action));
    return url;
}

QUrl BackendClient::buildTaskExecuteUrl(const QString &taskId) const
{
    QUrl url(baseUrl_);
    url.setPath(QStringLiteral("/api/tasks/%1/start").arg(QString::fromUtf8(QUrl::toPercentEncoding(taskId))));
    return url;
}

void BackendClient::handleTaskHistoryReply(QNetworkReply *reply)
{
    reply->deleteLater();

    if (reply->error() != QNetworkReply::NoError) {
        emit taskHistoryFailed(replyErrorMessage(reply));
        return;
    }

    // 列表页只需要摘要字段，完整步骤和日志留到用户选中任务后再查。
    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    const QJsonObject payload = document.object();
    const QJsonArray tasksArray = payload.value(QStringLiteral("tasks")).toArray();

    TaskHistoryResult result;
    result.total = payload.value(QStringLiteral("total")).toInt();
    result.limit = payload.value(QStringLiteral("limit")).toInt();
    result.offset = payload.value(QStringLiteral("offset")).toInt();
    result.tasks.reserve(tasksArray.size());

    for (const QJsonValue &value : tasksArray) {
        const QJsonObject object = value.toObject();
        TaskHistoryItem item;
        item.taskId = object.value(QStringLiteral("task_id")).toString();
        item.mode = object.value(QStringLiteral("mode")).toString();
        item.status = object.value(QStringLiteral("status")).toString();
        item.summary = object.value(QStringLiteral("summary")).toString();
        item.maxRiskLevel = object.value(QStringLiteral("max_risk_level")).toString();
        item.requiresConfirmation = object.value(QStringLiteral("requires_confirmation")).toBool();
        item.stepCount = object.value(QStringLiteral("step_count")).toInt();
        item.createdAt = object.value(QStringLiteral("created_at")).toString();
        item.updatedAt = object.value(QStringLiteral("updated_at")).toString();

        if (!item.taskId.isEmpty()) {
            result.tasks.append(item);
        }
    }

    emit taskHistoryReceived(result);
}

void BackendClient::handleModelProvidersReply(QNetworkReply *reply)
{
    reply->deleteLater();

    if (reply->error() != QNetworkReply::NoError) {
        emit modelProvidersFailed(replyErrorMessage(reply));
        return;
    }

    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    const QJsonObject payload = document.object();
    const ModelProviderListResult result = readModelProviderListResult(payload);
    if (result.providers.isEmpty()) {
        emit modelProvidersFailed(QStringLiteral("模型供应商响应为空，无法展示 provider profile。"));
        return;
    }

    emit modelProvidersReceived(result);
}

void BackendClient::handleModelRoutesReply(QNetworkReply *reply)
{
    reply->deleteLater();

    if (reply->error() != QNetworkReply::NoError) {
        emit modelRoutesFailed(replyErrorMessage(reply));
        return;
    }

    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    const ModelRouteListResult result = readModelRouteListResult(document.object());
    if (result.routes.isEmpty()) {
        emit modelRoutesFailed(QStringLiteral("任务模型路由响应为空。"));
        return;
    }

    emit modelRoutesReceived(result);
}

void BackendClient::handleModelRouteSaveReply(QNetworkReply *reply)
{
    reply->deleteLater();

    if (reply->error() != QNetworkReply::NoError) {
        emit modelRouteSaveFailed(replyErrorMessage(reply));
        return;
    }

    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    const ModelRouteInfo route = readModelRouteInfo(document.object());
    if (route.routeId.isEmpty()) {
        emit modelRouteSaveFailed(QStringLiteral("任务模型路由保存响应缺少 route_id。"));
        return;
    }

    emit modelRouteSaved(route);
}

void BackendClient::handleModelConfigSaveReply(QNetworkReply *reply)
{
    reply->deleteLater();

    if (reply->error() != QNetworkReply::NoError) {
        emit modelConfigSaveFailed(replyErrorMessage(reply));
        return;
    }

    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    const QJsonObject payload = document.object();
    const ModelProviderStatus status = readModelProviderStatus(payload);
    if (status.provider.isEmpty()) {
        emit modelConfigSaveFailed(QStringLiteral("模型配置保存响应缺少 provider。"));
        return;
    }

    emit modelConfigSaved(status);
}

void BackendClient::handleModelConnectionTestReply(QNetworkReply *reply)
{
    reply->deleteLater();

    if (reply->error() != QNetworkReply::NoError) {
        emit modelConnectionTestFailed(replyErrorMessage(reply));
        return;
    }

    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    const QJsonObject payload = document.object();
    const ModelConnectionTestResult result = readModelConnectionTestResult(payload);
    if (result.provider.isEmpty() || result.message.isEmpty()) {
        emit modelConnectionTestFailed(QStringLiteral("模型连接测试响应缺少 provider 或 message。"));
        return;
    }

    emit modelConnectionTestCompleted(result);
}

void BackendClient::handleWorkspaceDocumentImportReply(QNetworkReply *reply)
{
    reply->deleteLater();

    if (reply->error() != QNetworkReply::NoError) {
        emit workspaceDocumentImportFailed(replyErrorMessage(reply));
        return;
    }

    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    const WorkspaceDocumentInfo imported = readWorkspaceDocumentInfo(document.object());
    if (imported.name.isEmpty()) {
        emit workspaceDocumentImportFailed(QStringLiteral("workspace 文档导入响应缺少文件名。"));
        return;
    }

    emit workspaceDocumentImported(imported);
}

void BackendClient::handleWorkspaceDocumentsReply(QNetworkReply *reply)
{
    reply->deleteLater();

    if (reply->error() != QNetworkReply::NoError) {
        emit workspaceDocumentsFailed(replyErrorMessage(reply));
        return;
    }

    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    if (!document.isObject()) {
        emit workspaceDocumentsFailed(QStringLiteral("workspace 文档列表响应格式无效。"));
        return;
    }
    emit workspaceDocumentsReceived(readWorkspaceDocumentListResult(document.object()));
}

void BackendClient::handleKnowledgeBasesReply(QNetworkReply *reply)
{
    reply->deleteLater();
    if (reply->error() != QNetworkReply::NoError) {
        emit knowledgeBasesFailed(replyErrorMessage(reply));
        return;
    }
    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    if (!document.isObject() || !document.object().value(QStringLiteral("knowledge_bases")).isArray()) {
        emit knowledgeBasesFailed(QStringLiteral("资料库列表响应格式无效。"));
        return;
    }
    emit knowledgeBasesReceived(readKnowledgeBaseListResult(document.object()));
}

void BackendClient::handleKnowledgeBaseCreateReply(QNetworkReply *reply)
{
    reply->deleteLater();
    if (reply->error() != QNetworkReply::NoError) {
        emit knowledgeBaseCreateFailed(replyErrorMessage(reply));
        return;
    }
    const KnowledgeBaseInfo knowledgeBase = readKnowledgeBaseInfo(QJsonDocument::fromJson(reply->readAll()).object());
    if (knowledgeBase.knowledgeBaseId.isEmpty() || knowledgeBase.name.isEmpty()) {
        emit knowledgeBaseCreateFailed(QStringLiteral("新资料库响应缺少标识或名称。"));
        return;
    }
    emit knowledgeBaseCreated(knowledgeBase);
}

void BackendClient::handleKnowledgeDocumentsReply(QNetworkReply *reply)
{
    reply->deleteLater();
    if (reply->error() != QNetworkReply::NoError) {
        emit knowledgeDocumentsFailed(replyErrorMessage(reply));
        return;
    }
    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    if (!document.isObject() || document.object().value(QStringLiteral("knowledge_base_id")).toString().isEmpty()
        || !document.object().value(QStringLiteral("documents")).isArray()) {
        emit knowledgeDocumentsFailed(QStringLiteral("资料库材料列表响应格式无效。"));
        return;
    }
    emit knowledgeDocumentsReceived(readKnowledgeDocumentListResult(document.object()));
}

void BackendClient::handleKnowledgeDocumentImportReply(QNetworkReply *reply)
{
    reply->deleteLater();
    if (reply->error() != QNetworkReply::NoError) {
        emit knowledgeDocumentsImportFailed(replyErrorMessage(reply));
        return;
    }
    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    const QString knowledgeBaseId = document.object().value(QStringLiteral("knowledge_base_id")).toString();
    if (!document.isObject() || knowledgeBaseId.isEmpty() || !document.object().value(QStringLiteral("items")).isArray()) {
        emit knowledgeDocumentsImportFailed(QStringLiteral("资料库导入响应格式无效。"));
        return;
    }
    emit knowledgeDocumentsImported(knowledgeBaseId);
}

void BackendClient::handleKnowledgeIndexStartReply(QNetworkReply *reply)
{
    reply->deleteLater();
    if (reply->error() != QNetworkReply::NoError) {
        emit knowledgeIndexStartFailed(replyErrorMessage(reply));
        return;
    }
    const KnowledgeIndexJobInfo job = readKnowledgeIndexJobInfo(QJsonDocument::fromJson(reply->readAll()).object());
    if (job.indexJobId.isEmpty() || job.knowledgeBaseId.isEmpty()) {
        emit knowledgeIndexStartFailed(QStringLiteral("索引受理响应缺少任务标识。"));
        return;
    }
    emit knowledgeIndexStarted(job);
}

void BackendClient::handleKnowledgeIndexJobReply(QNetworkReply *reply)
{
    reply->deleteLater();
    if (reply->error() != QNetworkReply::NoError) {
        emit knowledgeIndexJobFailed(replyErrorMessage(reply));
        return;
    }
    const KnowledgeIndexJobInfo job = readKnowledgeIndexJobInfo(QJsonDocument::fromJson(reply->readAll()).object());
    if (job.indexJobId.isEmpty()) {
        emit knowledgeIndexJobFailed(QStringLiteral("索引状态响应缺少任务标识。"));
        return;
    }
    emit knowledgeIndexJobReceived(job);
}

void BackendClient::handleKnowledgeVectorCapabilityReply(QNetworkReply *reply)
{
    reply->deleteLater();
    if (reply->error() != QNetworkReply::NoError) {
        emit knowledgeVectorCapabilityFailed(replyErrorMessage(reply));
        return;
    }
    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    if (!document.isObject()) {
        emit knowledgeVectorCapabilityFailed(QStringLiteral("语义索引能力响应格式无效。"));
        return;
    }
    emit knowledgeVectorCapabilityReceived(readKnowledgeVectorCapabilityInfo(document.object()));
}

void BackendClient::handleKnowledgeVectorPrepareReply(QNetworkReply *reply)
{
    reply->deleteLater();
    if (reply->error() != QNetworkReply::NoError) {
        emit knowledgeVectorModelPrepareFailed(replyErrorMessage(reply));
        return;
    }
    const QJsonObject payload = QJsonDocument::fromJson(reply->readAll()).object();
    if (payload.value(QStringLiteral("status")).toString() != QStringLiteral("ready")) {
        emit knowledgeVectorModelPrepareFailed(QStringLiteral("本地语义模型准备响应格式无效。"));
        return;
    }
    emit knowledgeVectorModelPrepared(payload.value(QStringLiteral("message")).toString());
}

void BackendClient::handleKnowledgeOcrCapabilityReply(QNetworkReply *reply)
{
    reply->deleteLater();
    if (reply->error() != QNetworkReply::NoError) {
        emit knowledgeOcrCapabilityFailed(replyErrorMessage(reply));
        return;
    }
    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    if (!document.isObject()) {
        emit knowledgeOcrCapabilityFailed(QStringLiteral("本地 OCR 能力响应格式无效。"));
        return;
    }
    const KnowledgeOcrCapabilityInfo capability = readKnowledgeOcrCapabilityInfo(document.object());
    if (capability.profile.isEmpty()) {
        emit knowledgeOcrCapabilityFailed(QStringLiteral("本地 OCR 能力响应缺少模型档位。"));
        return;
    }
    emit knowledgeOcrCapabilityReceived(capability);
}

void BackendClient::handleKnowledgeOcrPrepareReply(QNetworkReply *reply)
{
    reply->deleteLater();
    if (reply->error() != QNetworkReply::NoError) {
        emit knowledgeOcrPreparationFailed(replyErrorMessage(reply));
        return;
    }
    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    if (!document.isObject()) {
        emit knowledgeOcrPreparationFailed(QStringLiteral("OCR 准备受理响应格式无效。"));
        return;
    }
    const KnowledgeOcrPreparationInfo preparation = readKnowledgeOcrPreparationInfo(document.object());
    if (preparation.preparationId.isEmpty() || preparation.status.isEmpty()) {
        emit knowledgeOcrPreparationFailed(QStringLiteral("OCR 准备受理响应缺少任务状态。"));
        return;
    }
    emit knowledgeOcrPreparationReceived(preparation);
}

void BackendClient::handleKnowledgeOcrPreparationReply(QNetworkReply *reply)
{
    reply->deleteLater();
    if (reply->error() != QNetworkReply::NoError) {
        emit knowledgeOcrPreparationFailed(replyErrorMessage(reply));
        return;
    }
    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    if (!document.isObject()) {
        emit knowledgeOcrPreparationFailed(QStringLiteral("OCR 准备状态响应格式无效。"));
        return;
    }
    const KnowledgeOcrPreparationInfo preparation = readKnowledgeOcrPreparationInfo(document.object());
    if (preparation.preparationId.isEmpty() || preparation.status.isEmpty()) {
        emit knowledgeOcrPreparationFailed(QStringLiteral("OCR 准备状态缺少必要字段。"));
        return;
    }
    emit knowledgeOcrPreparationReceived(preparation);
}

void BackendClient::handleKnowledgeBaseDeletionReply(QNetworkReply *reply)
{
    reply->deleteLater();
    if (reply->error() != QNetworkReply::NoError) {
        emit knowledgeBaseDeletionFailed(replyErrorMessage(reply));
        return;
    }
    const KnowledgeBaseInfo knowledgeBase = readKnowledgeBaseInfo(QJsonDocument::fromJson(reply->readAll()).object());
    if (knowledgeBase.knowledgeBaseId.isEmpty() || knowledgeBase.status != QStringLiteral("deleting")) {
        emit knowledgeBaseDeletionFailed(QStringLiteral("资料库删除受理响应格式无效。"));
        return;
    }
    emit knowledgeBaseDeletionRequested(knowledgeBase);
}

void BackendClient::handleKnowledgeAnswerStartReply(QNetworkReply *reply)
{
    reply->deleteLater();
    if (reply->error() != QNetworkReply::NoError) {
        emit knowledgeAnswerFailed(replyErrorMessage(reply));
        return;
    }
    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    if (!document.isObject()) {
        emit knowledgeAnswerFailed(QStringLiteral("知识库问答受理响应格式无效。"));
        return;
    }
    const KnowledgeAnswerTaskStartResult result = readKnowledgeAnswerTaskStartResult(document.object());
    if (result.taskId.isEmpty() || result.status != QStringLiteral("queued")) {
        emit knowledgeAnswerFailed(QStringLiteral("知识库问答未返回有效的受理状态。"));
        return;
    }
    emit knowledgeAnswerStarted(result);
}

void BackendClient::handleKnowledgeAnswerResultReply(QNetworkReply *reply)
{
    reply->deleteLater();
    if (reply->error() != QNetworkReply::NoError) {
        emit knowledgeAnswerFailed(replyErrorMessage(reply));
        return;
    }
    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    if (!document.isObject()) {
        emit knowledgeAnswerFailed(QStringLiteral("知识库问答结果响应格式无效。"));
        return;
    }
    const KnowledgeAnswerTaskResult result = readKnowledgeAnswerTaskResult(document.object());
    if (result.taskId.isEmpty() || result.status.isEmpty()) {
        emit knowledgeAnswerFailed(QStringLiteral("知识库问答结果缺少任务状态。"));
        return;
    }
    if (result.status == QStringLiteral("pending") || result.status == QStringLiteral("running")) {
        emit knowledgeAnswerStillRunning(result.taskId, result.status);
        return;
    }
    if (result.status == QStringLiteral("completed") && result.result.isEmpty()) {
        emit knowledgeAnswerFailed(QStringLiteral("知识库问答已结束，但没有通过验证的可展示结果。"));
        return;
    }
    emit knowledgeAnswerCompleted(result);
}

void BackendClient::handleKnowledgeDeepTaskStartReply(QNetworkReply *reply)
{
    reply->deleteLater();
    if (reply->error() != QNetworkReply::NoError) {
        emit knowledgeDeepTaskFailed(replyErrorMessage(reply));
        return;
    }
    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    if (!document.isObject()) {
        emit knowledgeDeepTaskFailed(QStringLiteral("深度分析任务受理响应格式无效。"));
        return;
    }
    const KnowledgeDeepTaskStartResult result = readKnowledgeDeepTaskStartResult(document.object());
    if (result.taskId.isEmpty() || result.status != QStringLiteral("queued")) {
        emit knowledgeDeepTaskFailed(QStringLiteral("深度分析任务未返回有效的受理状态。"));
        return;
    }
    emit knowledgeDeepTaskStarted(result);
}

void BackendClient::handleKnowledgeDeepTaskResultReply(QNetworkReply *reply)
{
    reply->deleteLater();
    if (reply->error() != QNetworkReply::NoError) {
        emit knowledgeDeepTaskFailed(replyErrorMessage(reply));
        return;
    }
    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    if (!document.isObject()) {
        emit knowledgeDeepTaskFailed(QStringLiteral("深度分析任务结果响应格式无效。"));
        return;
    }
    const KnowledgeDeepTaskResult result = readKnowledgeDeepTaskResult(document.object());
    if (result.taskId.isEmpty() || result.status.isEmpty()) {
        emit knowledgeDeepTaskFailed(QStringLiteral("深度分析任务结果缺少任务状态。"));
        return;
    }
    if (result.status == QStringLiteral("queued") || result.status == QStringLiteral("pending")
        || result.status == QStringLiteral("running")) {
        emit knowledgeDeepTaskStillRunning(result.taskId, result.status);
    }
    emit knowledgeDeepTaskResultReceived(result);
}

void BackendClient::handleKnowledgeDeepTaskControlReply(QNetworkReply *reply)
{
    reply->deleteLater();
    if (reply->error() != QNetworkReply::NoError) {
        emit knowledgeDeepTaskFailed(replyErrorMessage(reply));
        return;
    }
    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    if (!document.isObject()) {
        emit knowledgeDeepTaskFailed(QStringLiteral("深度分析任务控制响应格式无效。"));
        return;
    }
    const KnowledgeDeepTaskControlResult result = readKnowledgeDeepTaskControlResult(document.object());
    if (result.taskId.isEmpty() || result.action.isEmpty() || result.status.isEmpty()) {
        emit knowledgeDeepTaskFailed(QStringLiteral("深度分析任务控制响应缺少必要状态。"));
        return;
    }
    emit knowledgeDeepTaskControlCompleted(result);
}

void BackendClient::handleKnowledgeDeepTaskReportReply(QNetworkReply *reply)
{
    reply->deleteLater();
    if (reply->error() != QNetworkReply::NoError) {
        emit knowledgeDeepTaskFailed(replyErrorMessage(reply));
        return;
    }
    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    if (!document.isObject()) {
        emit knowledgeDeepTaskFailed(QStringLiteral("深度分析正式报告响应格式无效。"));
        return;
    }
    const KnowledgeDeepTaskReportExportResult result = readKnowledgeDeepTaskReportExportResult(document.object());
    if (result.taskId.isEmpty() || result.artifactId.isEmpty() || result.filename.isEmpty()) {
        emit knowledgeDeepTaskFailed(QStringLiteral("深度分析正式报告缺少受控产物信息。"));
        return;
    }
    emit knowledgeDeepTaskReportExported(result);
}

void BackendClient::handleDataDatasetImportReply(QNetworkReply *reply)
{
    reply->deleteLater();
    if (reply->error() != QNetworkReply::NoError) {
        emit dataDatasetImportFailed(replyErrorMessage(reply));
        return;
    }

    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    const DataDatasetInfo dataset = readDataDatasetInfo(document.object());
    if (dataset.name.isEmpty()) {
        emit dataDatasetImportFailed(QStringLiteral("数据导入响应缺少文件名。"));
        return;
    }
    emit dataDatasetImported(dataset);
}

void BackendClient::handleDataDatasetsReply(QNetworkReply *reply)
{
    reply->deleteLater();
    if (reply->error() != QNetworkReply::NoError) {
        emit dataDatasetsFailed(replyErrorMessage(reply));
        return;
    }

    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    if (!document.isObject()) {
        emit dataDatasetsFailed(QStringLiteral("数据文件列表响应格式无效。"));
        return;
    }
    emit dataDatasetsReceived(readDataDatasetListResult(document.object()));
}

void BackendClient::handleDataDatasetProfileReply(QNetworkReply *reply)
{
    reply->deleteLater();
    if (reply->error() != QNetworkReply::NoError) {
        emit dataDatasetProfileFailed(replyErrorMessage(reply));
        return;
    }

    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    if (!document.isObject() || document.object().value(QStringLiteral("selected_sheet")).toString().isEmpty()) {
        emit dataDatasetProfileFailed(QStringLiteral("数据画像响应缺少主工作表信息。"));
        return;
    }
    emit dataDatasetProfileReceived(document.object());
}

void BackendClient::handleDataRecommendationsReply(QNetworkReply *reply)
{
    reply->deleteLater();
    if (reply->error() != QNetworkReply::NoError) {
        emit dataRecommendationsFailed(replyErrorMessage(reply));
        return;
    }

    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    const QJsonObject payload = document.object();
    if (!document.isObject() || payload.value(QStringLiteral("dataset_name")).toString().isEmpty()
        || !payload.value(QStringLiteral("recommendations")).isArray()) {
        emit dataRecommendationsFailed(QStringLiteral("下一步建议响应格式无效。"));
        return;
    }
    emit dataRecommendationsReceived(payload);
}

void BackendClient::handleDataAnalysisPreviewReply(QNetworkReply *reply)
{
    reply->deleteLater();
    if (reply->error() != QNetworkReply::NoError) {
        emit dataAnalysisPreviewFailed(replyErrorMessage(reply));
        return;
    }

    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    if (!document.isObject() || !document.object().value(QStringLiteral("analysis_plan")).isObject()) {
        emit dataAnalysisPreviewFailed(QStringLiteral("分析预览响应缺少受控计划。"));
        return;
    }
    emit dataAnalysisPreviewReceived(document.object());
}

void BackendClient::handleDataAnalysisWorkbookExportStartReply(QNetworkReply *reply)
{
    reply->deleteLater();
    if (reply->error() != QNetworkReply::NoError) {
        emit dataAnalysisWorkbookExportFailed(replyErrorMessage(reply));
        return;
    }

    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    const QJsonObject payload = document.object();
    const QString taskId = payload.value(QStringLiteral("task_id")).toString().trimmed();
    if (!document.isObject() || taskId.isEmpty() || payload.value(QStringLiteral("status")).toString() != QStringLiteral("queued")) {
        emit dataAnalysisWorkbookExportFailed(QStringLiteral("Excel 导出任务未返回有效的受理状态。"));
        return;
    }
    emit dataAnalysisWorkbookExportStarted(taskId);
}

void BackendClient::handleDataAnalysisWorkbookExportResultReply(QNetworkReply *reply)
{
    reply->deleteLater();
    if (reply->error() != QNetworkReply::NoError) {
        emit dataAnalysisWorkbookExportFailed(replyErrorMessage(reply));
        return;
    }

    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    if (!document.isObject()) {
        emit dataAnalysisWorkbookExportFailed(QStringLiteral("Excel 导出结果响应格式无效。"));
        return;
    }
    const QJsonObject payload = document.object();
    const QString taskId = payload.value(QStringLiteral("task_id")).toString().trimmed();
    const QString status = payload.value(QStringLiteral("status")).toString();
    if (taskId.isEmpty() || status.isEmpty()) {
        emit dataAnalysisWorkbookExportFailed(QStringLiteral("Excel 导出结果缺少任务状态。"));
        return;
    }
    if (status == QStringLiteral("queued") || status == QStringLiteral("pending") || status == QStringLiteral("running")) {
        emit dataAnalysisWorkbookExportStillRunning(taskId, status);
        return;
    }
    if (status == QStringLiteral("cancelled")) {
        emit dataAnalysisWorkbookExportCancelled(
            payload.value(QStringLiteral("message")).toString(
                QStringLiteral("Excel 导出已取消，未登记新的交付文件。")));
        return;
    }
    if (status == QStringLiteral("completed")
        && payload.value(QStringLiteral("artifact")).isObject()
        && payload.value(QStringLiteral("verification")).toObject().value(QStringLiteral("passed")).toBool()) {
        emit dataAnalysisWorkbookExported(payload);
        return;
    }
    emit dataAnalysisWorkbookExportFailed(
        payload.value(QStringLiteral("message")).toString(
            QStringLiteral("Excel 导出未完成，请在任务历史中查看原因。")));
}

void BackendClient::handleDataChartExportStartReply(QNetworkReply *reply)
{
    reply->deleteLater();
    if (reply->error() != QNetworkReply::NoError) {
        emit dataChartExportFailed(replyErrorMessage(reply));
        return;
    }
    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    const QJsonObject payload = document.object();
    const QString taskId = payload.value(QStringLiteral("task_id")).toString().trimmed();
    if (!document.isObject() || taskId.isEmpty() || payload.value(QStringLiteral("status")).toString() != QStringLiteral("queued")) {
        emit dataChartExportFailed(QStringLiteral("图表看板任务未返回有效的受理状态。"));
        return;
    }
    emit dataChartExportStarted(taskId);
}

void BackendClient::handleDataChartExportResultReply(QNetworkReply *reply)
{
    reply->deleteLater();
    if (reply->error() != QNetworkReply::NoError) {
        emit dataChartExportFailed(replyErrorMessage(reply));
        return;
    }
    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    if (!document.isObject()) {
        emit dataChartExportFailed(QStringLiteral("图表看板结果响应格式无效。"));
        return;
    }
    const QJsonObject payload = document.object();
    const QString taskId = payload.value(QStringLiteral("task_id")).toString().trimmed();
    const QString status = payload.value(QStringLiteral("status")).toString();
    if (taskId.isEmpty() || status.isEmpty()) {
        emit dataChartExportFailed(QStringLiteral("图表看板结果缺少任务状态。"));
        return;
    }
    if (status == QStringLiteral("queued") || status == QStringLiteral("pending") || status == QStringLiteral("running")) {
        emit dataChartExportStillRunning(taskId, status);
        return;
    }
    if (status == QStringLiteral("cancelled")) {
        emit dataChartExportCancelled(payload.value(QStringLiteral("message")).toString(
            QStringLiteral("图表看板生成已取消，未登记新的 PNG。")));
        return;
    }
    if (status == QStringLiteral("completed")
        && payload.value(QStringLiteral("artifacts")).isArray()
        && !payload.value(QStringLiteral("artifacts")).toArray().isEmpty()
        && payload.value(QStringLiteral("verification")).toObject().value(QStringLiteral("passed")).toBool()) {
        emit dataChartExported(payload);
        return;
    }
    emit dataChartExportFailed(payload.value(QStringLiteral("message")).toString(
        QStringLiteral("图表看板未完成，请在任务历史中查看原因。")));
}

void BackendClient::handleDataTransformationPreviewReply(QNetworkReply *reply)
{
    reply->deleteLater();
    if (reply->error() != QNetworkReply::NoError) {
        emit dataTransformationPreviewFailed(replyErrorMessage(reply));
        return;
    }
    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    const QJsonObject payload = document.object();
    if (!document.isObject() || !payload.value(QStringLiteral("plan")).isObject()) {
        emit dataTransformationPreviewFailed(QStringLiteral("字段加工预览响应格式无效。"));
        return;
    }
    emit dataTransformationPreviewReceived(payload);
}

void BackendClient::handleDataTransformationExportStartReply(QNetworkReply *reply)
{
    reply->deleteLater();
    if (reply->error() != QNetworkReply::NoError) {
        emit dataTransformationExportFailed(replyErrorMessage(reply));
        return;
    }
    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    const QJsonObject payload = document.object();
    const QString taskId = payload.value(QStringLiteral("task_id")).toString().trimmed();
    if (!document.isObject() || taskId.isEmpty()
        || payload.value(QStringLiteral("status")).toString() != QStringLiteral("queued")) {
        emit dataTransformationExportFailed(QStringLiteral("字段加工任务未返回有效的受理状态。"));
        return;
    }
    emit dataTransformationExportStarted(taskId);
}

void BackendClient::handleDataTransformationExportResultReply(QNetworkReply *reply)
{
    reply->deleteLater();
    if (reply->error() != QNetworkReply::NoError) {
        emit dataTransformationExportFailed(replyErrorMessage(reply));
        return;
    }
    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    if (!document.isObject()) {
        emit dataTransformationExportFailed(QStringLiteral("字段加工结果响应格式无效。"));
        return;
    }
    const QJsonObject payload = document.object();
    const QString taskId = payload.value(QStringLiteral("task_id")).toString().trimmed();
    const QString status = payload.value(QStringLiteral("status")).toString();
    if (taskId.isEmpty() || status.isEmpty()) {
        emit dataTransformationExportFailed(QStringLiteral("字段加工结果缺少任务状态。"));
        return;
    }
    if (status == QStringLiteral("queued") || status == QStringLiteral("pending") || status == QStringLiteral("running")) {
        emit dataTransformationExportStillRunning(taskId, status);
        return;
    }
    if (status == QStringLiteral("cancelled")) {
        emit dataTransformationExportCancelled(payload.value(QStringLiteral("message")).toString(
            QStringLiteral("字段加工已取消，未登记新的数据副本。")));
        return;
    }
    if (status == QStringLiteral("completed")
        && payload.value(QStringLiteral("artifact")).isObject()
        && payload.value(QStringLiteral("plan")).isObject()
        && payload.value(QStringLiteral("verification")).toObject().value(QStringLiteral("passed")).toBool()) {
        emit dataTransformationExported(payload);
        return;
    }
    emit dataTransformationExportFailed(payload.value(QStringLiteral("message")).toString(
        QStringLiteral("字段加工未完成，请在任务历史中查看原因。")));
}

void BackendClient::handleDocumentAgentStartReply(QNetworkReply *reply)
{
    reply->deleteLater();

    if (reply->error() != QNetworkReply::NoError) {
        emit documentAgentFailed(replyErrorMessage(reply));
        return;
    }

    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    if (!document.isObject()) {
        emit documentAgentFailed(QStringLiteral("文档助手受理响应格式无效。"));
        return;
    }
    const DocumentAgentTaskStartResult result = readDocumentAgentTaskStartResult(document.object());
    if (result.taskId.isEmpty() || result.status != QStringLiteral("queued")) {
        emit documentAgentFailed(QStringLiteral("文档助手未返回有效的任务受理状态。"));
        return;
    }
    emit documentAgentStarted(result);
}

void BackendClient::handleDocumentAgentResultReply(QNetworkReply *reply)
{
    reply->deleteLater();

    if (reply->error() != QNetworkReply::NoError) {
        emit documentAgentFailed(replyErrorMessage(reply));
        return;
    }

    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    if (!document.isObject()) {
        emit documentAgentFailed(QStringLiteral("文档助手结果响应格式无效。"));
        return;
    }
    const QJsonObject payload = document.object();
    const QString taskId = payload.value(QStringLiteral("task_id")).toString();
    const QString status = payload.value(QStringLiteral("status")).toString();
    const QJsonObject resultPayload = payload.value(QStringLiteral("result")).toObject();
    if (!resultPayload.isEmpty()) {
        const DocumentAgentRunResult result = readDocumentAgentRunResult(resultPayload);
        if (result.taskId.isEmpty() || result.status.isEmpty() || result.reply.isEmpty()) {
            emit documentAgentFailed(QStringLiteral("文档助手结果缺少任务状态或结论。"));
            return;
        }
        emit documentAgentCompleted(result);
        return;
    }
    if (!taskId.isEmpty() && (status == QStringLiteral("queued") || status == QStringLiteral("running"))) {
        emit documentAgentStillRunning(taskId, status);
        return;
    }
    emit documentAgentFailed(QStringLiteral("文档助手未返回可展示的终态结果。"));
}

void BackendClient::handlePdfProcessingStartReply(QNetworkReply *reply)
{
    reply->deleteLater();

    if (reply->error() != QNetworkReply::NoError) {
        emit pdfProcessingFailed(replyErrorMessage(reply));
        return;
    }
    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    if (!document.isObject()) {
        emit pdfProcessingFailed(QStringLiteral("PDF 整理任务受理响应格式无效。"));
        return;
    }
    const PdfProcessingTaskStartResult result = readPdfProcessingTaskStartResult(document.object());
    if (result.taskId.isEmpty() || result.status != QStringLiteral("queued")) {
        emit pdfProcessingFailed(QStringLiteral("PDF 整理未返回有效的任务受理状态。"));
        return;
    }
    emit pdfProcessingStarted(result);
}

void BackendClient::handlePdfProcessingResultReply(QNetworkReply *reply)
{
    reply->deleteLater();

    if (reply->error() != QNetworkReply::NoError) {
        emit pdfProcessingFailed(replyErrorMessage(reply));
        return;
    }
    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    if (!document.isObject()) {
        emit pdfProcessingFailed(QStringLiteral("PDF 整理结果响应格式无效。"));
        return;
    }
    const PdfProcessingTaskResult result = readPdfProcessingTaskResult(document.object());
    if (result.taskId.isEmpty() || result.status.isEmpty()) {
        emit pdfProcessingFailed(QStringLiteral("PDF 整理结果缺少任务状态。"));
        return;
    }
    if (result.status == QStringLiteral("queued") || result.status == QStringLiteral("pending")
        || result.status == QStringLiteral("running")) {
        emit pdfProcessingStillRunning(result.taskId, result.status);
        return;
    }
    if (result.status == QStringLiteral("completed") && !result.hasArtifact) {
        emit pdfProcessingFailed(QStringLiteral("PDF 整理已结束，但没有找到可交付的输出文件。"));
        return;
    }
    emit pdfProcessingCompleted(result);
}

void BackendClient::handleDocumentDraftSaveReply(QNetworkReply *reply)
{
    reply->deleteLater();

    if (reply->error() != QNetworkReply::NoError) {
        emit documentDraftSaveFailed(replyErrorMessage(reply));
        return;
    }

    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    if (!document.isObject()) {
        emit documentDraftSaveFailed(QStringLiteral("Markdown 草稿保存响应格式无效。"));
        return;
    }
    const DocumentDraftSaveResult result = readDocumentDraftSaveResult(document.object());
    if (result.taskId.isEmpty() || result.artifactId.isEmpty() || result.relativePath.isEmpty()) {
        emit documentDraftSaveFailed(QStringLiteral("Markdown 草稿保存响应缺少产物信息。"));
        return;
    }
    emit documentDraftSaved(result);
}

void BackendClient::handlePresentationPreviewReply(QNetworkReply *reply)
{
    reply->deleteLater();
    if (reply->error() != QNetworkReply::NoError) {
        emit presentationPreviewFailed(replyErrorMessage(reply));
        return;
    }

    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    if (!document.isObject()) {
        emit presentationPreviewFailed(QStringLiteral("项目方案 PPT 预览响应格式无效。"));
        return;
    }
    const PresentationPreviewResult result = readPresentationPreviewResult(document.object());
    if (result.sourceTaskId.isEmpty() || result.planId.isEmpty() || result.slides.size() < 3) {
        emit presentationPreviewFailed(QStringLiteral("项目方案 PPT 预览缺少计划或幻灯片内容。"));
        return;
    }
    emit presentationPreviewReceived(result);
}

void BackendClient::handlePresentationExportReply(QNetworkReply *reply)
{
    reply->deleteLater();
    if (reply->error() != QNetworkReply::NoError) {
        emit presentationExportFailed(replyErrorMessage(reply));
        return;
    }

    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    if (!document.isObject()) {
        emit presentationExportFailed(QStringLiteral("项目方案 PPT 导出响应格式无效。"));
        return;
    }
    const PresentationExportResult result = readPresentationExportResult(document.object());
    if (result.taskId.isEmpty() || result.artifactId.isEmpty() || result.relativePath.isEmpty()
        || result.slideCount <= 0 || !result.verification.value(QStringLiteral("passed")).toBool()) {
        emit presentationExportFailed(QStringLiteral("项目方案 PPT 导出未通过交付验证。"));
        return;
    }
    emit presentationExported(result);
}

void BackendClient::handlePresentationStudioStartReply(QNetworkReply *reply)
{
    reply->deleteLater();
    if (reply->error() != QNetworkReply::NoError) {
        emit presentationStudioFailed(replyErrorMessage(reply));
        return;
    }
    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    if (!document.isObject()) {
        emit presentationStudioFailed(QStringLiteral("PPT 创作任务受理响应格式无效。"));
        return;
    }
    const PresentationStudioTaskStartResult result = readPresentationStudioTaskStartResult(document.object());
    if (result.taskId.isEmpty() || result.status != QStringLiteral("queued")) {
        emit presentationStudioFailed(QStringLiteral("PPT 创作任务受理响应缺少任务身份。"));
        return;
    }
    emit presentationStudioStarted(result);
}

void BackendClient::handlePresentationStudioResultReply(QNetworkReply *reply)
{
    reply->deleteLater();
    if (reply->error() != QNetworkReply::NoError) {
        emit presentationStudioFailed(replyErrorMessage(reply));
        return;
    }
    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    if (!document.isObject()) {
        emit presentationStudioFailed(QStringLiteral("PPT 创作计划响应格式无效。"));
        return;
    }
    const QJsonObject payload = document.object();
    const QString taskId = payload.value(QStringLiteral("task_id")).toString();
    const QString status = payload.value(QStringLiteral("status")).toString();
    if (taskId.isEmpty()) {
        emit presentationStudioFailed(QStringLiteral("PPT 创作计划响应缺少任务 ID。"));
        return;
    }
    if (status == QStringLiteral("running")) {
        emit presentationStudioStillRunning(taskId, status);
        return;
    }
    if (status != QStringLiteral("completed")) {
        emit presentationStudioFailed(QStringLiteral("PPT 创作计划没有完成，请在任务历史查看后重试。"));
        return;
    }
    const PresentationStudioPlanResult result = readPresentationStudioPlanResult(
        payload.value(QStringLiteral("result")).toObject());
    if (result.taskId.isEmpty() || result.planId.isEmpty() || result.slides.size() < 5) {
        emit presentationStudioFailed(QStringLiteral("PPT 创作计划缺少可确认的简报或页面。"));
        return;
    }
    emit presentationStudioPlanReceived(result);
}

void BackendClient::handlePresentationStudioExportReply(QNetworkReply *reply)
{
    reply->deleteLater();
    if (reply->error() != QNetworkReply::NoError) {
        emit presentationStudioExportFailed(replyErrorMessage(reply));
        return;
    }
    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    if (!document.isObject()) {
        emit presentationStudioExportFailed(QStringLiteral("PPT 创作导出响应格式无效。"));
        return;
    }
    const PresentationExportResult result = readPresentationExportResult(document.object());
    if (result.taskId.isEmpty() || result.artifactId.isEmpty() || result.relativePath.isEmpty()
        || result.slideCount <= 0 || !result.verification.value(QStringLiteral("passed")).toBool()) {
        emit presentationStudioExportFailed(QStringLiteral("PPT 创作导出未通过交付验证。"));
        return;
    }
    emit presentationStudioExported(result);
}

void BackendClient::handlePresentationStudioExportPrepareReply(QNetworkReply *reply)
{
    reply->deleteLater();
    if (reply->error() != QNetworkReply::NoError) {
        emit presentationStudioExportFailed(replyErrorMessage(reply));
        return;
    }
    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    const QString taskId = document.object().value(QStringLiteral("task_id")).toString();
    if (taskId.isEmpty()) {
        emit presentationStudioExportFailed(QStringLiteral("PPT 导出状态通道响应缺少 task_id。"));
        return;
    }
    emit presentationStudioExportPrepared(taskId);
}

void BackendClient::handleProjectReviewReply(QNetworkReply *reply)
{
    reply->deleteLater();
    if (reply->error() != QNetworkReply::NoError) {
        emit projectReviewFailed(replyErrorMessage(reply));
        return;
    }

    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    if (!document.isObject()) {
        emit projectReviewFailed(QStringLiteral("项目审查响应格式无效。"));
        return;
    }
    const ProjectReviewResult result = readProjectReviewResult(document.object());
    if (result.taskId.isEmpty() || result.status != QStringLiteral("completed")
        || result.report.value(QStringLiteral("checks")).toArray().isEmpty()) {
        emit projectReviewFailed(QStringLiteral("项目审查没有返回可展示的质量检查报告。"));
        return;
    }
    emit projectReviewReceived(result);
}

void BackendClient::handleProjectReviewStartReply(QNetworkReply *reply)
{
    reply->deleteLater();
    if (reply->error() != QNetworkReply::NoError) {
        emit projectReviewFailed(replyErrorMessage(reply));
        return;
    }

    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    const QString taskId = document.object().value(QStringLiteral("task_id")).toString().trimmed();
    if (taskId.isEmpty()) {
        emit projectReviewFailed(QStringLiteral("项目审查受理响应缺少任务 ID。"));
        return;
    }
    emit projectReviewStarted(taskId);
}

void BackendClient::handleProjectReviewResultReply(QNetworkReply *reply)
{
    reply->deleteLater();
    if (reply->error() != QNetworkReply::NoError) {
        emit projectReviewFailed(replyErrorMessage(reply));
        return;
    }

    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    if (!document.isObject()) {
        emit projectReviewFailed(QStringLiteral("项目审查结果响应格式无效。"));
        return;
    }
    const QJsonObject payload = document.object();
    const QString taskId = payload.value(QStringLiteral("task_id")).toString().trimmed();
    const QString status = payload.value(QStringLiteral("status")).toString();
    const QJsonObject resultPayload = payload.value(QStringLiteral("result")).toObject();
    if (!resultPayload.isEmpty()) {
        const ProjectReviewResult result = readProjectReviewResult(resultPayload);
        if (result.taskId.isEmpty() || result.status != QStringLiteral("completed")
            || result.report.value(QStringLiteral("checks")).toArray().isEmpty()) {
            emit projectReviewFailed(QStringLiteral("项目审查没有返回可展示的质量检查报告。"));
            return;
        }
        emit projectReviewReceived(result);
        return;
    }
    if (!taskId.isEmpty() && (status == QStringLiteral("queued") || status == QStringLiteral("running"))) {
        emit projectReviewStillRunning(taskId, status);
        return;
    }
    emit projectReviewFailed(QStringLiteral("项目审查未返回可展示的终态报告。"));
}

void BackendClient::handlePaperReviewReply(QNetworkReply *reply)
{
    reply->deleteLater();
    if (reply->error() != QNetworkReply::NoError) {
        emit paperReviewFailed(replyErrorMessage(reply));
        return;
    }

    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    if (!document.isObject()) {
        emit paperReviewFailed(QStringLiteral("论文审查响应格式无效。"));
        return;
    }
    const PaperReviewResult result = readPaperReviewResult(document.object());
    if (result.taskId.isEmpty() || result.status != QStringLiteral("completed")
        || result.report.value(QStringLiteral("checks")).toArray().isEmpty()) {
        emit paperReviewFailed(QStringLiteral("论文审查没有返回可展示的规则报告。"));
        return;
    }
    emit paperReviewReceived(result);
}

void BackendClient::handlePaperReviewStartReply(QNetworkReply *reply)
{
    reply->deleteLater();
    if (reply->error() != QNetworkReply::NoError) {
        emit paperReviewFailed(replyErrorMessage(reply));
        return;
    }

    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    const QString taskId = document.object().value(QStringLiteral("task_id")).toString().trimmed();
    if (taskId.isEmpty()) {
        emit paperReviewFailed(QStringLiteral("论文审查受理响应缺少任务 ID。"));
        return;
    }
    emit paperReviewStarted(taskId);
}

void BackendClient::handlePaperReviewResultReply(QNetworkReply *reply)
{
    reply->deleteLater();
    if (reply->error() != QNetworkReply::NoError) {
        emit paperReviewFailed(replyErrorMessage(reply));
        return;
    }

    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    if (!document.isObject()) {
        emit paperReviewFailed(QStringLiteral("论文审查结果响应格式无效。"));
        return;
    }
    const QJsonObject payload = document.object();
    const QString taskId = payload.value(QStringLiteral("task_id")).toString().trimmed();
    const QString status = payload.value(QStringLiteral("status")).toString();
    const QJsonObject resultPayload = payload.value(QStringLiteral("result")).toObject();
    if (!resultPayload.isEmpty()) {
        const PaperReviewResult result = readPaperReviewResult(resultPayload);
        if (result.taskId.isEmpty() || result.status != QStringLiteral("completed")
            || result.report.value(QStringLiteral("checks")).toArray().isEmpty()) {
            emit paperReviewFailed(QStringLiteral("论文审查没有返回可展示的规则报告。"));
            return;
        }
        emit paperReviewReceived(result);
        return;
    }
    if (!taskId.isEmpty() && (status == QStringLiteral("queued") || status == QStringLiteral("running"))) {
        emit paperReviewStillRunning(taskId, status);
        return;
    }
    emit paperReviewFailed(QStringLiteral("论文审查未返回可展示的终态报告。"));
}

void BackendClient::handleTaskLogsReply(QNetworkReply *reply)
{
    reply->deleteLater();

    if (reply->error() != QNetworkReply::NoError) {
        emit taskLogsFailed(replyErrorMessage(reply));
        return;
    }

    // 日志列表本身也是一条轻量回放链路，只保留 task_id 和 message 解析结果。
    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    const QJsonObject payload = document.object();
    const QJsonArray eventsArray = payload.value(QStringLiteral("events")).toArray();

    TaskLogListResult result;
    result.taskId = payload.value(QStringLiteral("task_id")).toString();
    result.total = payload.value(QStringLiteral("total")).toInt();
    result.events.reserve(eventsArray.size());

    for (const QJsonValue &value : eventsArray) {
        const TaskLogEvent event = readTaskLogEvent(value.toObject());
        if (!event.taskId.isEmpty() && !event.message.isEmpty()) {
            result.events.append(event);
        }
    }

    emit taskLogsReceived(result);
}

void BackendClient::handleTaskPlanReply(QNetworkReply *reply)
{
    reply->deleteLater();

    if (reply->error() != QNetworkReply::NoError) {
        emit taskPlanFailed(replyErrorMessage(reply));
        return;
    }

    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    const QJsonObject payload = document.object();
    const WorkflowPlanDetailResult result = readWorkflowPlanDetailResult(payload);
    if (result.taskId.isEmpty()) {
        emit taskPlanFailed(QStringLiteral("总指挥计划响应缺少 task_id。"));
        return;
    }

    emit taskPlanReceived(result);
}

void BackendClient::handleTaskPlanVersionsReply(QNetworkReply *reply)
{
    reply->deleteLater();

    if (reply->error() != QNetworkReply::NoError) {
        emit taskPlanVersionsFailed(replyErrorMessage(reply));
        return;
    }

    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    const WorkflowPlanVersionListResult result = readWorkflowPlanVersionListResult(document.object());
    if (result.taskId.isEmpty()) {
        emit taskPlanVersionsFailed(QStringLiteral("计划版本响应缺少 task_id。"));
        return;
    }

    emit taskPlanVersionsReceived(result);
}

void BackendClient::handleTaskPlanVersionReply(QNetworkReply *reply)
{
    reply->deleteLater();

    if (reply->error() != QNetworkReply::NoError) {
        emit taskPlanVersionFailed(replyErrorMessage(reply));
        return;
    }

    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    const WorkflowPlanDetailResult result = readWorkflowPlanDetailResult(document.object());
    if (result.taskId.isEmpty() || result.planSummary.planVersion < 1) {
        emit taskPlanVersionFailed(QStringLiteral("计划版本详情响应不完整。"));
        return;
    }

    emit taskPlanVersionReceived(result);
}

void BackendClient::handleTaskPlanRevisionReply(QNetworkReply *reply)
{
    reply->deleteLater();

    if (reply->error() != QNetworkReply::NoError) {
        emit taskPlanRevisionFailed(replyErrorMessage(reply));
        return;
    }

    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    const WorkflowPlanRevisionResult result = readWorkflowPlanRevisionResult(document.object());
    if (result.taskId.isEmpty() || result.planSummary.planVersion < 1) {
        emit taskPlanRevisionFailed(QStringLiteral("计划修订响应不完整，请刷新版本列表后重试。"));
        return;
    }

    emit taskPlanRevisionCompleted(result);
}

void BackendClient::handleTaskStepsReply(QNetworkReply *reply)
{
    reply->deleteLater();

    if (reply->error() != QNetworkReply::NoError) {
        emit taskStepsFailed(replyErrorMessage(reply));
        return;
    }

    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    const QJsonObject payload = document.object();
    const TaskStepListResult result = readTaskStepListResult(payload);
    if (result.taskId.isEmpty()) {
        emit taskStepsFailed(QStringLiteral("任务步骤响应缺少 task_id。"));
        return;
    }

    emit taskStepsReceived(result);
}

void BackendClient::handleTaskPermissionsReply(QNetworkReply *reply)
{
    reply->deleteLater();

    if (reply->error() != QNetworkReply::NoError) {
        emit taskPermissionsFailed(replyErrorMessage(reply));
        return;
    }

    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    const QJsonObject payload = document.object();
    const RuntimePermissionListResult result = readRuntimePermissionListResult(payload);
    if (result.taskId.isEmpty()) {
        emit taskPermissionsFailed(QStringLiteral("权限请求响应缺少 task_id。"));
        return;
    }

    emit taskPermissionsReceived(result);
}

void BackendClient::handleTaskRuntimeStateReply(QNetworkReply *reply)
{
    reply->deleteLater();

    if (reply->error() != QNetworkReply::NoError) {
        emit taskRuntimeStateFailed(replyErrorMessage(reply));
        return;
    }

    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    const QJsonObject payload = document.object();
    const WorkflowRuntimeStateInfo result = readWorkflowRuntimeStateInfo(payload);
    if (result.taskId.isEmpty()) {
        emit taskRuntimeStateFailed(QStringLiteral("运行态响应缺少 task_id。"));
        return;
    }

    emit taskRuntimeStateReceived(result);
}

void BackendClient::handleTaskMetricsReply(QNetworkReply *reply)
{
    reply->deleteLater();

    if (reply->error() != QNetworkReply::NoError) {
        emit taskMetricsFailed(replyErrorMessage(reply));
        return;
    }

    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    const QJsonObject payload = document.object();
    const WorkflowRuntimeMetricsResult result = readWorkflowRuntimeMetricsResult(payload);
    if (result.taskId.isEmpty()) {
        emit taskMetricsFailed(QStringLiteral("运行指标响应缺少 task_id。"));
        return;
    }

    emit taskMetricsReceived(result);
}

void BackendClient::handleTaskModelRoutesReply(QNetworkReply *reply)
{
    reply->deleteLater();

    if (reply->error() != QNetworkReply::NoError) {
        emit taskModelRoutesFailed(replyErrorMessage(reply));
        return;
    }

    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    const WorkflowModelRouteAuditResult result = readWorkflowModelRouteAuditResult(document.object());
    if (result.taskId.isEmpty()) {
        emit taskModelRoutesFailed(QStringLiteral("实际模型审计响应缺少 task_id。"));
        return;
    }

    emit taskModelRoutesReceived(result);
}

void BackendClient::handleTaskEvaluationReply(QNetworkReply *reply)
{
    reply->deleteLater();

    if (reply->error() != QNetworkReply::NoError) {
        emit taskEvaluationFailed(replyErrorMessage(reply));
        return;
    }

    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    const QJsonObject payload = document.object();
    const WorkflowTaskEvaluationResult result = readWorkflowTaskEvaluationResult(payload);
    if (result.taskId.isEmpty()) {
        emit taskEvaluationFailed(QStringLiteral("任务评估响应缺少 task_id。"));
        return;
    }

    emit taskEvaluationReceived(result);
}

void BackendClient::handleNodeContractsReply(QNetworkReply *reply)
{
    reply->deleteLater();

    if (reply->error() != QNetworkReply::NoError) {
        emit nodeContractsFailed(replyErrorMessage(reply));
        return;
    }

    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    const QJsonObject payload = document.object();
    const WorkflowNodeContractListResult result = readWorkflowNodeContractListResult(payload);
    if (result.contracts.isEmpty()) {
        emit nodeContractsFailed(QStringLiteral("节点契约响应为空，无法解释工作流步骤。"));
        return;
    }

    emit nodeContractsReceived(result);
}

void BackendClient::handleWorkflowCommandPolicyReply(QNetworkReply *reply)
{
    reply->deleteLater();

    if (reply->error() != QNetworkReply::NoError) {
        emit workflowCommandPolicyCheckFailed(replyErrorMessage(reply));
        return;
    }

    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    const QJsonObject payload = document.object();
    const WorkflowCommandPolicyCheckResult result = readWorkflowCommandPolicyCheckResult(payload);
    if (result.command.isEmpty()) {
        emit workflowCommandPolicyCheckFailed(QStringLiteral("命令安全检查响应缺少 command。"));
        return;
    }

    emit workflowCommandPolicyChecked(result);
}

void BackendClient::handleRuntimePreferencesReply(QNetworkReply *reply)
{
    reply->deleteLater();

    if (reply->error() != QNetworkReply::NoError) {
        emit runtimePreferencesFailed(replyErrorMessage(reply));
        return;
    }

    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    const RuntimePreferencesResult result = readRuntimePreferencesResult(document.object());
    if (result.permissionPolicy.isEmpty() || result.personality.isEmpty()) {
        emit runtimePreferencesFailed(QStringLiteral("运行偏好响应缺少权限模式或 Agent 风格。"));
        return;
    }

    emit runtimePreferencesReceived(result);
}

void BackendClient::handleRuntimePreferencesSaveReply(QNetworkReply *reply)
{
    reply->deleteLater();

    if (reply->error() != QNetworkReply::NoError) {
        emit runtimePreferencesSaveFailed(replyErrorMessage(reply));
        return;
    }

    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    const RuntimePreferencesResult result = readRuntimePreferencesResult(document.object());
    if (result.permissionPolicy.isEmpty() || result.personality.isEmpty()) {
        emit runtimePreferencesSaveFailed(QStringLiteral("运行偏好保存响应缺少权限模式或 Agent 风格。"));
        return;
    }

    emit runtimePreferencesSaved(result);
}

void BackendClient::handleLongTermMemoriesReply(QNetworkReply *reply)
{
    reply->deleteLater();
    if (reply->error() != QNetworkReply::NoError) {
        emit longTermMemoriesFailed(replyErrorMessage(reply));
        return;
    }

    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    if (!document.isObject()) {
        emit longTermMemoriesFailed(QStringLiteral("长期记忆列表响应格式无效。"));
        return;
    }
    QList<LongTermMemoryInfo> items;
    for (const QJsonValue &value : document.object().value(QStringLiteral("items")).toArray()) {
        if (!value.isObject()) {
            continue;
        }
        const LongTermMemoryInfo item = readLongTermMemoryInfo(value.toObject());
        if (!item.memoryId.isEmpty()) {
            items.append(item);
        }
    }
    emit longTermMemoriesReceived(items);
}

void BackendClient::handleLongTermMemoryMutationReply(
    QNetworkReply *reply,
    const QString &successMessage)
{
    reply->deleteLater();
    if (reply->error() != QNetworkReply::NoError) {
        emit longTermMemoryMutationFailed(replyErrorMessage(reply));
        return;
    }
    emit longTermMemoryMutationCompleted(successMessage);
}

void BackendClient::handleTaskArtifactsReply(QNetworkReply *reply)
{
    reply->deleteLater();

    if (reply->error() != QNetworkReply::NoError) {
        emit taskArtifactsFailed(replyErrorMessage(reply));
        return;
    }

    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    const QJsonObject payload = document.object();
    const WorkflowArtifactListResult result = readWorkflowArtifactListResult(payload);
    if (result.taskId.isEmpty()) {
        emit taskArtifactsFailed(QStringLiteral("产物响应缺少 task_id。"));
        return;
    }

    emit taskArtifactsReceived(result);
}

void BackendClient::handleTaskArtifactPreviewReply(
    QNetworkReply *reply,
    const QString &requestedTaskId,
    const QString &requestedArtifactId)
{
    reply->deleteLater();

    if (reply->error() != QNetworkReply::NoError) {
        emit taskArtifactPreviewFailed(requestedTaskId, requestedArtifactId, replyErrorMessage(reply));
        return;
    }

    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    const QJsonObject payload = document.object();
    const WorkflowArtifactPreviewResult result = readWorkflowArtifactPreviewResult(payload);
    if (result.taskId.isEmpty() || result.artifactId.isEmpty()) {
        emit taskArtifactPreviewFailed(
            requestedTaskId,
            requestedArtifactId,
            QStringLiteral("产物预览响应缺少 task_id 或 artifact_id。"));
        return;
    }

    emit taskArtifactPreviewReceived(result);
}

void BackendClient::handleTaskArtifactOpenReply(
    QNetworkReply *reply,
    const QString &requestedTaskId,
    const QString &requestedArtifactId)
{
    reply->deleteLater();

    if (reply->error() != QNetworkReply::NoError) {
        emit taskArtifactOpenFailed(requestedTaskId, requestedArtifactId, replyErrorMessage(reply));
        return;
    }

    const QJsonObject payload = QJsonDocument::fromJson(reply->readAll()).object();
    const QString taskId = payload.value(QStringLiteral("task_id")).toString();
    const QString artifactId = payload.value(QStringLiteral("artifact_id")).toString();
    if (taskId.isEmpty() || artifactId.isEmpty() || !payload.value(QStringLiteral("opened")).toBool()) {
        emit taskArtifactOpenFailed(
            requestedTaskId,
            requestedArtifactId,
            QStringLiteral("打开产物响应缺少有效的任务、产物或打开状态。"));
        return;
    }

    emit taskArtifactOpened(taskId, artifactId,
                             payload.value(QStringLiteral("message")).toString(QStringLiteral("已打开产物。")));
}

void BackendClient::handleTaskToolCallsReply(QNetworkReply *reply)
{
    reply->deleteLater();

    if (reply->error() != QNetworkReply::NoError) {
        emit taskToolCallsFailed(replyErrorMessage(reply));
        return;
    }

    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    const QJsonObject payload = document.object();
    const WorkflowToolCallListResult result = readWorkflowToolCallListResult(payload);
    if (result.taskId.isEmpty()) {
        emit taskToolCallsFailed(QStringLiteral("工具调用响应缺少 task_id。"));
        return;
    }

    emit taskToolCallsReceived(result);
}

void BackendClient::handleTaskUpdatesReply(QNetworkReply *reply, const QString &requestedTaskId)
{
    reply->deleteLater();

    if (reply->error() != QNetworkReply::NoError) {
        emit taskUpdatesFailed(requestedTaskId, replyErrorMessage(reply));
        return;
    }

    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    const QJsonObject payload = document.object();
    const WorkflowTaskUpdateListResult result = readWorkflowTaskUpdateListResult(payload);
    if (result.taskId.isEmpty()) {
        emit taskUpdatesFailed(requestedTaskId, QStringLiteral("事件流响应缺少 task_id。"));
        return;
    }

    emit taskUpdatesReceived(result);
}

void BackendClient::handleTaskDeliveryCardReply(
    QNetworkReply *reply,
    const QString &requestedTaskId)
{
    reply->deleteLater();

    if (reply->error() != QNetworkReply::NoError) {
        emit taskDeliveryCardFailed(requestedTaskId, replyErrorMessage(reply));
        return;
    }

    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    if (!document.isObject()) {
        emit taskDeliveryCardFailed(requestedTaskId, QStringLiteral("交付结果响应格式无效。"));
        return;
    }

    const WorkflowDeliveryCardInfo card = readWorkflowDeliveryCardInfo(document.object());
    if (card.taskId.isEmpty() || card.schemaVersion.isEmpty()) {
        emit taskDeliveryCardFailed(requestedTaskId, QStringLiteral("交付结果缺少任务或协议版本。"));
        return;
    }
    emit taskDeliveryCardReceived(card);
}

void BackendClient::handleTaskPermissionDecisionReply(QNetworkReply *reply)
{
    reply->deleteLater();

    if (reply->error() != QNetworkReply::NoError) {
        emit taskPermissionDecisionFailed(replyErrorMessage(reply));
        return;
    }

    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    const QJsonObject payload = document.object();
    const RuntimePermissionItem item = readRuntimePermissionItem(payload);
    if (item.request.requestId.isEmpty() || item.decision.requestId.isEmpty()) {
        emit taskPermissionDecisionFailed(QStringLiteral("权限决策响应缺少 request_id。"));
        return;
    }

    emit taskPermissionDecisionCompleted(item);
}

void BackendClient::handleTaskControlReply(QNetworkReply *reply)
{
    reply->deleteLater();

    if (reply->error() != QNetworkReply::NoError) {
        emit taskControlFailed(replyErrorMessage(reply));
        return;
    }

    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    const QJsonObject payload = document.object();
    const TaskControlResult result = readTaskControlResult(payload);
    if (result.taskId.isEmpty() || result.action.isEmpty()) {
        emit taskControlFailed(QStringLiteral("任务控制响应缺少 task_id 或 action。"));
        return;
    }

    emit taskControlCompleted(result);
}

void BackendClient::handleTaskExecutionReply(QNetworkReply *reply)
{
    reply->deleteLater();

    if (reply->error() != QNetworkReply::NoError) {
        emit taskExecutionFailed(replyErrorMessage(reply));
        return;
    }

    const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
    const QJsonObject payload = document.object();
    const WorkflowExecutionResult result = readWorkflowExecutionResult(payload);
    if (result.sourceTaskId.isEmpty() || result.runtimeTaskId.isEmpty()) {
        emit taskExecutionFailed(QStringLiteral("执行响应缺少 source_task_id 或 runtime_task_id。"));
        return;
    }

    emit taskExecutionCompleted(result);
}

void BackendClient::handleTaskLogMessage(const QString &message)
{
    // 后端 WebSocket 每条消息都是 TaskLogEvent JSON。
    // 解析失败时 document.object() 为空，下面的有效性检查会丢弃坏消息。
    const QJsonDocument document = QJsonDocument::fromJson(message.toUtf8());
    const QJsonObject payload = document.object();

    const TaskLogEvent event = readTaskLogEvent(payload);

    if (!event.taskId.isEmpty() && !event.message.isEmpty()) {
        emit taskLogReceived(event);
    }
}
