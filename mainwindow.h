#ifndef MAINWINDOW_H
#define MAINWINDOW_H

#include <QMainWindow>
#include <QPushButton>
#include <QList>
#include <QLabel>
#include <QHash>
#include <QJsonArray>
#include <QJsonObject>
#include <QPixmap>
#include <QPointer>

#include "backendclient.h"
#include "backendmanager.h"

QT_BEGIN_NAMESPACE
namespace Ui {
class MainWindow;
}
QT_END_NAMESPACE

class QComboBox;
class QCloseEvent;
class QAction;
class QCheckBox;
class QDialog;
class TaskActivityIndicator;
class QLineEdit;
class QListWidget;
class QPlainTextEdit;
class QResizeEvent;
class QScrollArea;
class QTimer;
class QTableWidget;
class QTextBrowser;
class QTextEdit;
class QToolButton;
class QWidget;
class ModelRouteDialog;
class DispatchMaterialDialog;
class PresentationStudioDialog;

class MainWindow : public QMainWindow
{
    Q_OBJECT

public:
    explicit MainWindow(QWidget *parent = nullptr);
    ~MainWindow() override;

protected:
    void closeEvent(QCloseEvent *event) override;
    void resizeEvent(QResizeEvent *event) override;

private:
    Ui::MainWindow *ui;
    BackendClient *backendClient;
    BackendManager *backendManager;
    // 折叠仅改变导航信息密度，不改变当前页面、权限状态或各 Agent 的可见性定义。
    bool sidebarCollapsed = false;
    bool agentsNavigationExpanded = true;
    bool managementNavigationExpanded = true;

private:
    void setupBackendIntegration();
    void setupNavigation();
    QList<QPushButton *> navigationButtons() const;
    void loadNavigationPresentationPreferences();
    void saveNavigationPresentationPreferences() const;
    void setSidebarCollapsed(bool collapsed);
    void updateSidebarNavigationPresentation();
    void switchPage(int index);
    void setActiveNavButton(QPushButton *activeButton);
    void updateHeader(const QString &title, const QString &subtitle);
    void setBackendConnectingState();
    void updateBackendHealth(bool ok, const QString &message);
    void updateAgentCards(const QList<AgentInfo> &agents);
    void updateAgentCardSet(const QList<AgentInfo> &agents, int baseIndex, int cardCount);
    void setAgentCard(int index, const QString &title, const QString &subtitle, const QString &badge);
    QLabel *agentTitleLabel(int index) const;
    QLabel *agentSubtitleLabel(int index) const;
    QLabel *agentBadgeLabel(int index) const;
    void setupDispatchChat();
    QJsonArray buildDispatchMaterialBindings() const;
    QJsonArray buildDispatchAgentHints() const;
    void updateDispatchAgentHintsUi();
    void insertDispatchAgentHint(const QString &agentId);
    void removeDispatchAgentHint(const QString &agentId);
    void submitDispatchMessage(const QString &message,
                               const QJsonArray &materials,
                               const QString &projectScope,
                               const QJsonArray &agentHints);
    void queueDispatchMessageUntilBackendReady(const QString &message,
                                               const QJsonArray &materials,
                                               const QString &projectScope,
                                               const QJsonArray &agentHints);
    void flushQueuedDispatchMessage();
    void restoreQueuedDispatchMessage(const QString &reason);
    void loadDispatchConversationPreference();
    void saveDispatchConversationPreference() const;
    void startNewDispatchConversation();
    void openDispatchConversationHistory();
    void openDispatchConversationArchive();
    void selectDispatchConversation(const QString &conversationId);
    void requestDispatchConversationContext();
    void handleDispatchConversationContext(const ConversationContextInfo &context);
    void handleDispatchConversationContextFailed(const QString &message);
    void handleDispatchConversationSessions(const ConversationSessionListResult &result);
    void handleDispatchConversationSessionsFailed(const QString &message);
    void handleDispatchConversationTranscript(const ConversationTranscriptPageResult &result);
    void handleDispatchConversationTranscriptFailed(const QString &conversationId, const QString &message);
    // 安全只读的单材料任务不应把客户带进“计划 -> 预演 -> 再确认”的控制流。
    // 这里仅识别已经通过后端准入的知识库问答与数据预览；写入、联网、深度分析
    // 或多个专业分支仍沿用原有计划与人工确认边界。
    bool isCurrentDispatchDirectKnowledgeAnswer() const;
    bool isCurrentDispatchDirectDataAnalysis() const;
    bool isCurrentDispatchDataChartDelivery() const;
    bool isCurrentDispatchDataWorkbookDelivery() const;
    bool isCurrentDispatchAutoReadOnlyTask() const;
    bool isCurrentDispatchDirectConversation() const;
    QString currentDispatchAutoReadOnlyActivityText() const;
    QString currentDispatchKnowledgeBaseName() const;
    QString currentDispatchKnowledgeAnswerTaskId() const;
    void beginCurrentDispatchRuntime(bool automaticallyApproved);
    void requestCurrentDispatchKnowledgeAnswerResult();
    void handleDispatchKnowledgeAnswerCompleted(const KnowledgeAnswerTaskResult &result);
    void handleDispatchKnowledgeAnswerFailed(const QString &message);
    QString formatDispatchKnowledgeAnswerHtml(const KnowledgeAnswerTaskResult &result) const;
    QString formatDispatchAnswerMarkdownHtml(const QString &markdown) const;
    QString formatDispatchAssistantMessageHtml(const QString &markdown) const;
    QString formatDispatchUserMessageHtml(const QString &message) const;
    QString formatDispatchKnowledgeSourcesHtml(const QJsonArray &sources) const;
    void setDispatchActivityRunning(bool running);
    void importWorkspaceDocumentFromFile();
    void openDispatchMaterialDialog();
    void refreshDispatchMaterialCatalog();
    void updateDispatchMaterialCatalogStatus();
    void importDocumentAgentDocument();
    void importWorkspaceDocumentForTarget(const QString &target);
    void handleWorkspaceDocumentImported(const WorkspaceDocumentInfo &document);
    void handleWorkspaceDocumentImportFailed(const QString &message);
    void setupKnowledgeBase();
    void refreshKnowledgeBases();
    void selectKnowledgeBaseFromList();
    void refreshSelectedKnowledgeDocuments();
    void updateKnowledgeBaseDetailUi();
    void createKnowledgeBase();
    void importKnowledgeBaseDocument();
    void startKnowledgeIndex();
    void refreshKnowledgeIndexJob();
    void prepareKnowledgeVectorModel();
    void prepareKnowledgeOcrModel();
    void refreshKnowledgeOcrPreparation();
    void updateKnowledgeOcrUi();
    void deleteSelectedKnowledgeBase();
    void openKnowledgeAnswerDialog();
    void openKnowledgeDeepTaskDialog();
    void openKnowledgeDeepTaskDialogForExistingTask(const QString &taskId);
    void delegateKnowledgeBaseToCommander();
    void handleKnowledgeBasesReceived(const KnowledgeBaseListResult &result);
    void handleKnowledgeBasesFailed(const QString &message);
    void handleKnowledgeBaseCreated(const KnowledgeBaseInfo &knowledgeBase);
    void handleKnowledgeBaseCreateFailed(const QString &message);
    void handleKnowledgeDocumentsReceived(const KnowledgeDocumentListResult &result);
    void handleKnowledgeDocumentsFailed(const QString &message);
    void handleKnowledgeDocumentsImported(const QString &knowledgeBaseId);
    void handleKnowledgeDocumentsImportFailed(const QString &message);
    void handleKnowledgeIndexStarted(const KnowledgeIndexJobInfo &job);
    void handleKnowledgeIndexStartFailed(const QString &message);
    void handleKnowledgeIndexJobReceived(const KnowledgeIndexJobInfo &job);
    void handleKnowledgeIndexJobFailed(const QString &message);
    void handleKnowledgeVectorCapabilityReceived(const KnowledgeVectorCapabilityInfo &capability);
    void handleKnowledgeVectorCapabilityFailed(const QString &message);
    void handleKnowledgeVectorModelPrepared(const QString &message);
    void handleKnowledgeVectorModelPrepareFailed(const QString &message);
    void handleKnowledgeOcrCapabilityReceived(const KnowledgeOcrCapabilityInfo &capability);
    void handleKnowledgeOcrCapabilityFailed(const QString &message);
    void handleKnowledgeOcrPreparationReceived(const KnowledgeOcrPreparationInfo &preparation);
    void handleKnowledgeOcrPreparationFailed(const QString &message);
    void handleKnowledgeBaseDeletionRequested(const KnowledgeBaseInfo &knowledgeBase);
    void handleKnowledgeBaseDeletionFailed(const QString &message);
    void setupDataWorkspace();
    void importDataDatasetFromFile();
    void refreshDataDatasets();
    void requestSelectedDataDatasetProfile();
    void requestDataRecommendations();
    void requestDataAnalysisPreview();
    void delegateDataDatasetToCommander();
    void requestDataAnalysisWorkbookExport();
    void requestDataChartExport();
    void showDataTransformationWizard();
    void requestDataTransformationPreview(const QJsonObject &request);
    void requestDataTransformationExport();
    void handleDataDatasetImported(const DataDatasetInfo &dataset);
    void handleDataDatasetImportFailed(const QString &message);
    void handleDataDatasetsReceived(const DataDatasetListResult &result);
    void handleDataDatasetsFailed(const QString &message);
    void handleDataDatasetProfileReceived(const QJsonObject &profile);
    void handleDataDatasetProfileFailed(const QString &message);
    void handleDataRecommendationsReceived(const QJsonObject &recommendations);
    void handleDataRecommendationsFailed(const QString &message);
    void handleDataAnalysisPreviewReceived(const QJsonObject &preview);
    void handleDataAnalysisPreviewFailed(const QString &message);
    void handleDataAnalysisWorkbookExportStarted(const QString &taskId);
    void handleDataAnalysisWorkbookExported(const QJsonObject &result);
    void handleDataAnalysisWorkbookExportStillRunning(const QString &taskId, const QString &status);
    void handleDataAnalysisWorkbookExportCancelled(const QString &message);
    void handleDataAnalysisWorkbookExportFailed(const QString &message);
    void handleDataChartExportStarted(const QString &taskId);
    void handleDataChartExported(const QJsonObject &result);
    void handleDataChartExportStillRunning(const QString &taskId, const QString &status);
    void handleDataChartExportCancelled(const QString &message);
    void handleDataChartExportFailed(const QString &message);
    void handleDataChartImageReceived(const QString &taskId, const QString &artifactId, const QByteArray &imageBytes);
    void handleDataChartImageFailed(const QString &taskId, const QString &artifactId, const QString &message);
    void showDataAnalysisPreviewDialog();
    void showDataChartDashboard(const QJsonObject &result);
    void renderDataChartDashboardPixmap(const QPixmap &pixmap);
    QString formatDataAnalysisPreviewHtml() const;
    void handleDataTransformationPreviewReceived(const QJsonObject &preview);
    void handleDataTransformationPreviewFailed(const QString &message);
    void handleDataTransformationExportStarted(const QString &taskId);
    void handleDataTransformationExported(const QJsonObject &result);
    void handleDataTransformationExportStillRunning(const QString &taskId, const QString &status);
    void handleDataTransformationExportCancelled(const QString &message);
    void handleDataTransformationExportFailed(const QString &message);
    void showDataTransformationPreviewDialog();
    QString formatDataTransformationPreviewHtml() const;
    // 文档与数据工作台复用同一真实状态语义：只要已有后台状态处于运行，固定尺寸指示器才旋转。
    // 它不依据文案猜测进度，也不额外发起轮询、模型或文件请求。
    void updateDocumentActivityState();
    void updateDataActivityState();
    void setupDocumentAgent();
    void refreshDocumentAgentDocuments();
    void updateDocumentAgentSelectionUi();
    QStringList selectedDocumentAgentReferences() const;
    void openPresentationStudio();
    void openPresentationStudioForPrompt(const QString &prompt, bool directGenerate = false);
    void beginDocumentPresentationDraft();
    QString selectedDocumentForReview(const QString &dialogTitle, const QString &prompt);
    void runDocumentAgent();
    void handleWorkspaceDocumentsReceived(const WorkspaceDocumentListResult &result);
    void handleWorkspaceDocumentsFailed(const QString &message);
    void handleDocumentAgentStarted(const DocumentAgentTaskStartResult &result);
    void handleDocumentAgentCompleted(const DocumentAgentRunResult &result);
    void handleDocumentAgentStillRunning(const QString &taskId, const QString &status);
    void handleDocumentAgentFailed(const QString &message);
    void createDocumentDraftSectionPreview();
    void reviewDocumentDraftSection();
    void createDocumentDraftSectionRevisionPreview();
    void createDocumentDraftSectionBatchRevisionPreview();
    void createDocumentDraftSectionManualRevisionPreview();
    void beginDocumentManualRevisionPreview(const QString &statusText);
    void createDocumentDraftTemplatePreview();
    void beginDocumentDraftTemplatePreview(const QString &statusText);
    void createDocumentDraftMergePreview();
    void beginDocumentDraftMergePreview(const QString &statusText);
    void handleDocumentDraftMergeCandidatesReceived(const QJsonObject &result);
    void handleDocumentDraftMergePlanReceived(const QJsonObject &result);
    void handleDocumentDraftMergeFailed(const QString &message);
    void showDocumentDraftMergePlanDialog(const QJsonObject &result);
    void beginDocumentSectionRevisionPreview(const QString &statusText);
    void restoreDocumentDraftPreview();
    void beginDocumentDraftRestorePreview(const QString &statusText);
    void showDocumentDraftParentDiff();
    void handleDocumentDraftParentDiffReceived(const QJsonObject &result);
    void handleDocumentDraftParentDiffFailed(const QString &message);
    void showDocumentDraftParentDiffDialog(const QJsonObject &result);
    void reviewDocumentDraft();
    void copyDocumentDraftToClipboard();
    void saveDocumentDraft();
    void handleDocumentDraftSaved(const DocumentDraftSaveResult &result);
    void handleDocumentDraftSaveFailed(const QString &message);
    void requestDocumentPresentationPreview();
    void handlePresentationPreviewReceived(const PresentationPreviewResult &result);
    void handlePresentationPreviewFailed(const QString &message);
    void handlePresentationExported(const PresentationExportResult &result);
    void handlePresentationExportFailed(const QString &message);
    void showPresentationPreviewDialog(const PresentationPreviewResult &result);
    QString formatPresentationPreviewHtml(const PresentationPreviewResult &result) const;
    QString suggestedPresentationFilename() const;
    void requestProjectDocumentReview();
    void handleProjectReviewStarted(const QString &taskId);
    void handleProjectReviewStillRunning(const QString &taskId, const QString &status);
    void handleProjectReviewReceived(const ProjectReviewResult &result);
    void handleProjectReviewFailed(const QString &message);
    void showProjectReviewDialog(const ProjectReviewResult &result);
    QString formatProjectReviewHtml(const ProjectReviewResult &result) const;
    void requestPaperReview();
    void handlePaperReviewStarted(const QString &taskId);
    void handlePaperReviewStillRunning(const QString &taskId, const QString &status);
    void handlePaperReviewReceived(const PaperReviewResult &result);
    void handlePaperReviewFailed(const QString &message);
    void showPaperReviewDialog(const PaperReviewResult &result);
    void showLatestDocumentWorkbenchResult();
    void updateDocumentOpenResultAction();
    void updateProjectReviewAction();
    void updateDocumentDraftSaveAction();
    void updateDocumentPresentationAction();
    void updateDocumentSectionDraftAction();
    void updateDocumentDraftReviewAction();
    QString suggestedDocumentDraftFilename() const;
    QString documentOutputModeValue() const;
    bool documentOutputUsesMultipleMaterials() const;
    void updateDocumentAgentTaskHint();
    void showPdfProcessingWorkspace();
    void refreshPdfProcessingWorkspaceDocuments();
    void updatePdfProcessingWorkspaceUi();
    void startPdfProcessingTask();
    void handlePdfProcessingStarted(const PdfProcessingTaskStartResult &result);
    void handlePdfProcessingCompleted(const PdfProcessingTaskResult &result);
    void handlePdfProcessingStillRunning(const QString &taskId, const QString &status);
    void handlePdfProcessingFailed(const QString &message);
    void openPdfProcessingArtifact();
    QString formatDocumentAgentResultHtml(const DocumentAgentRunResult &result) const;
    void setDocumentResultHtml(const QString &html, const QString &detailStatus, bool available);
    void updateDocumentResultDetailSections(
        const QJsonObject &documentContext,
        bool available,
        const QString &firstSectionText = QStringLiteral("本次结论"),
        const QString &firstSectionLookup = QString());
    void showDocumentResultDetail();
    void showDocumentWorkbench();
    void refreshCurrentDispatchUpdates();
    void scheduleDispatchUpdatesRefresh(int delayMs = 250);
    bool shouldPollCurrentDispatchUpdates() const;
    void updateDispatchActionButtons();
    void openTaskInHistory(const QString &taskId);
    void openCurrentDispatchTaskInHistory();
    void executeCurrentDispatchTaskFromDispatch();
    void openDispatchPlanManager();
    void refreshDispatchPlanVersions();
    void handleDispatchPlanVersionsReceived(const WorkflowPlanVersionListResult &result);
    void handleDispatchPlanVersionsFailed(const QString &message);
    void handleDispatchPlanVersionReceived(const WorkflowPlanDetailResult &result);
    void handleDispatchPlanVersionFailed(const QString &message);
    void submitDispatchPlanRevision();
    void handleDispatchPlanRevisionCompleted(const WorkflowPlanRevisionResult &result);
    void handleDispatchPlanRevisionFailed(const QString &message);
    void updateDispatchPlanRevisionEditor();
    void configureDispatchProjectScope();
    void updateDispatchProjectScopeButton();
    void updateDispatchMaterialBindingsUi();
    void sendDispatchMessage();
    void handleChatCompleted(const ChatResult &result);
    void handleChatFailed(const QString &message);
    void handleTaskDeliveryCardReceived(const WorkflowDeliveryCardInfo &card);
    void handleTaskDeliveryCardFailed(const QString &taskId, const QString &message);
    void resetDispatchDeliveryCard();
    void requestCurrentDispatchDeliveryCardIfTerminal();
    void openDispatchDeliveryArtifact();
    void showDispatchDeliveryDialog(const WorkflowDeliveryCardInfo &card);
    void renderDispatchDeliveryImage();
    QString formatDispatchDeliveryCardHtml(const WorkflowDeliveryCardInfo &card) const;
    QString formatDispatchWorkflowPlanHtml(const ChatResult &result) const;
    QString formatDispatchWorkflowPlanHtml(
        const WorkflowPlanSummaryInfo &plan,
        const QList<WorkflowStepInfo> &steps) const;
    QString formatDispatchChatPlanCardHtml(
        const WorkflowPlanSummaryInfo &plan,
        const QList<WorkflowStepInfo> &steps) const;
    QString formatDispatchPlanSummaryHtml(const WorkflowPlanSummaryInfo &plan) const;
    QString formatDispatchStepContractHtml(const WorkflowStepInfo &step) const;
    void applyDispatchTaskUpdates(const WorkflowTaskUpdateListResult &result);
    void updateDispatchProgressFromLogEvent(const TaskLogEvent &event);
    QString formatDispatchUpdateHighlightHtml(const WorkflowTaskUpdateInfo &item) const;
    QString dispatchStatusTextForState(const QString &mode, const QString &status) const;
    void handleTaskLogReceived(const TaskLogEvent &event);
    void handleTaskLogFinished(const QString &taskId);
    void handleTaskLogFailed(const QString &message);
    void setupCodeWorkshop();
    void checkCodeWorkshopCommandPolicy();
    void handleWorkflowCommandPolicyChecked(const WorkflowCommandPolicyCheckResult &result);
    void handleWorkflowCommandPolicyCheckFailed(const QString &message);
    QString commandPolicyRiskText(const QString &riskLevel) const;
    QString commandExecutionRouteText(const QString &route) const;
    QString commandPolicyBadgeObjectName(const QString &riskLevel, bool allowed) const;
    QString formatCommandPolicyResultHtml(const WorkflowCommandPolicyCheckResult &result) const;
    void setupSettingsPage();
    void refreshRuntimePreferences();
    void saveRuntimePreferencesFromSettings();
    void handleRuntimePreferencesReceived(const RuntimePreferencesResult &result);
    void handleRuntimePreferencesFailed(const QString &message);
    void handleRuntimePreferencesSaved(const RuntimePreferencesResult &result);
    void handleRuntimePreferencesSaveFailed(const QString &message);
    void applyRuntimePreferencesToSettings(const RuntimePreferencesResult &result);
    QString runtimePermissionPolicyText(const QString &value) const;
    QString runtimePersonalityText(const QString &value) const;
    QString formatRuntimePreferencesNotesHtml(const RuntimePreferencesResult &result) const;
    void openLongTermMemoryManager();
    void refreshLongTermMemoryManager();
    void handleLongTermMemoriesReceived(const QList<LongTermMemoryInfo> &items);
    void handleLongTermMemoriesFailed(const QString &message);
    void handleLongTermMemoryMutationCompleted(const QString &message);
    void handleLongTermMemoryMutationFailed(const QString &message);
    void populateLongTermMemoryEditor(const LongTermMemoryInfo *item);
    QString longTermMemoryKindText(const QString &kind) const;
    void setupHistoryPage();
    void refreshTaskHistory();
    void handleTaskHistoryReceived(const TaskHistoryResult &result);
    void handleTaskHistoryFailed(const QString &message);
    void handleTaskPlanReceived(const WorkflowPlanDetailResult &result);
    void handleTaskPlanFailed(const QString &message);
    void requestHistoryMemoryProposal();
    void handleTaskMemoryProposalsReceived(const TaskMemoryProposalListResult &result);
    void handleTaskMemoryProposalsFailed(const QString &taskId, const QString &message);
    void handleTaskMemoryProposalConfirmed(const QString &taskId, const QString &message);
    void handleTaskMemoryProposalConfirmFailed(const QString &taskId, const QString &message);
    void handleTaskStepsReceived(const TaskStepListResult &result);
    void handleTaskStepsFailed(const QString &message);
    void handleTaskRuntimeStateReceived(const WorkflowRuntimeStateInfo &result);
    void handleTaskRuntimeStateFailed(const QString &message);
    void handleTaskMetricsReceived(const WorkflowRuntimeMetricsResult &result);
    void handleTaskMetricsFailed(const QString &message);
    void handleTaskModelRoutesReceived(const WorkflowModelRouteAuditResult &result);
    void handleTaskModelRoutesFailed(const QString &message);
    void handleTaskEvaluationReceived(const WorkflowTaskEvaluationResult &result);
    void handleTaskEvaluationFailed(const QString &message);
    void handleNodeContractsReceived(const WorkflowNodeContractListResult &result);
    void handleNodeContractsFailed(const QString &message);
    void handleTaskArtifactsReceived(const WorkflowArtifactListResult &result);
    void handleTaskArtifactsFailed(const QString &message);
    void handleTaskToolCallsReceived(const WorkflowToolCallListResult &result);
    void handleTaskToolCallsFailed(const QString &message);
    void handleTaskUpdatesReceived(const WorkflowTaskUpdateListResult &result);
    void handleTaskUpdatesFailed(const QString &taskId, const QString &message);
    void setupModelPage();
    void refreshModelProviders();
    void openModelRouteDialog();
    void openModelRouteDialogForRoute(const QString &routeId);
    void updateDispatchModelRoutePresentation(const ModelRouteInfo &route);
    void updateSpecialistModelRoutePresentations();
    QString modelRoutePresentationText(const QString &routeId) const;
    void handleModelProvidersReceived(const ModelProviderListResult &result);
    void handleModelProvidersFailed(const QString &message);
    void handleModelRoutesReceived(const ModelRouteListResult &result);
    void handleModelRoutesFailed(const QString &message);
    void handleModelRouteSaved(const ModelRouteInfo &route);
    void handleModelRouteSaveFailed(const QString &message);
    void applyModelKeywordFilter();
    void onModelProviderSelectionChanged();
    bool selectModelProviderRowById(const QString &providerId);
    void updateModelSummaryPanel();
    void updateModelDetailPanel();
    void updateModelConfigForm();
    void updateModelConfigButtons();
    void saveSelectedModelConfig(bool clearKey = false);
    void testSelectedModelConnection();
    void handleModelConfigSaved(const ModelProviderStatus &status);
    void handleModelConfigSaveFailed(const QString &message);
    void handleModelConnectionTestCompleted(const ModelConnectionTestResult &result);
    void handleModelConnectionTestFailed(const QString &message);
    void showModelEmptyState(const QString &message);
    QString modelTransportText(const QString &transport) const;
    QString modelProviderBadgeObjectName(const QString &providerId) const;
    QString formatModelProviderDetailHtml(const ModelProviderInfo &provider) const;
    void handleTaskLogsReceived(const TaskLogListResult &result);
    void handleTaskLogsFailed(const QString &message);
    void handleTaskPermissionsReceived(const RuntimePermissionListResult &result);
    void handleTaskPermissionsFailed(const QString &message);
    void handleTaskPermissionDecisionCompleted(const RuntimePermissionItem &item);
    void handleTaskPermissionDecisionFailed(const QString &message);
    void handleTaskControlCompleted(const TaskControlResult &result);
    void handleTaskControlFailed(const QString &message);
    void handleTaskExecutionCompleted(const WorkflowExecutionResult &result);
    void handleTaskExecutionFailed(const QString &message);
    void handleTaskArtifactPreviewReceived(const WorkflowArtifactPreviewResult &result);
    void handleTaskArtifactPreviewFailed(const QString &taskId, const QString &artifactId, const QString &message);
    void handleTaskArtifactOpened(const QString &taskId, const QString &artifactId, const QString &message);
    void handleTaskArtifactOpenFailed(const QString &taskId, const QString &artifactId, const QString &message);
    void onHistoryFilterChanged();
    void onHistoryRowSelectionChanged();
    void applyHistoryKeywordFilter();
    bool confirmHistoryExecutionRequest();
    void updateHistoryPagerControls();
    void updateHistoryActionButtons();
    void restoreHistoryDocumentDraftPreview();
    void updateHistoryRuntimePanel();
    void setupHistoryAutoRefresh();
    void setupHistoryArtifactToolbar();
    void refreshCurrentHistoryDetails();
    void updateHistoryAutoRefreshState();
    bool shouldAutoRefreshCurrentHistoryTask() const;
    QJsonObject currentHistoryDelegation() const;
    void refreshHistoryArtifactToolbar();
    void onHistoryArtifactSelectionChanged();
    void updateHistoryArtifactActionState();
    void updateHistoryModelRoutesButton();
    void showHistoryModelRoutesDialog();
    QString historyModelRouteStageText(const QString &stage) const;
    QString historyModelRouteSummaryText() const;
    void previewSelectedHistoryArtifact();
    void openSelectedHistoryArtifact();
    void copySelectedHistoryArtifactPath();
    const WorkflowArtifactInfo *selectedHistoryArtifact() const;
    QString historyArtifactLocalPath(const WorkflowArtifactInfo &artifact) const;
    QString historyArtifactDelegatedTaskId(const WorkflowArtifactInfo &artifact) const;
    QString historyArtifactPreviewText(const WorkflowArtifactInfo &artifact) const;
    QString historyArtifactPreviewText(
        const WorkflowArtifactInfo &artifact,
        const WorkflowArtifactPreviewResult &preview) const;
    void showHistoryArtifactPreviewDialog(const WorkflowArtifactInfo &artifact, const QString &previewText);
    void showHistoryConfirmationLoading();
    void refreshHistoryDetailPanel();
    void updateHistoryConfirmationPanel(const QList<RuntimePermissionItem> &permissions);
    void updateHistoryConfirmationPanelFromLogs(const QList<TaskLogEvent> &events);
    void refreshHistorySelectionBadge();
    void markHistoryConfirmationAcknowledged();
    void approveNextHistoryPermission();
    void setHistoryConfirmationExpanded(bool expanded);
    bool selectHistoryRowByTaskId(const QString &taskId);
    void showHistoryEmptyState(const QString &message);
    QString historyPermissionDecisionText(const QString &decision) const;
    QString historyPermissionBadgeObjectName(const QString &decision) const;
    QString formatHistoryPermissionsHtml() const;
    QString formatHistoryPermissionItemHtml(const RuntimePermissionItem &item) const;
    QString formatHistoryRuntimeStateHtml() const;
    QString formatHistoryMetricsHtml() const;
    QString formatHistoryMetricsSummaryText() const;
    QString formatHistoryEvaluationHtml() const;
    QString nodeContractKey(const QString &agentId, const QString &action) const;
    const WorkflowNodeContractInfo *nodeContractForStep(const QString &agentId, const QString &action) const;
    const WorkflowNodeContractInfo *nodeContractForToolName(const QString &toolName) const;
    QString formatHistoryNodeContractHtml(const WorkflowNodeContractInfo *contract) const;
    QString formatHistoryArtifactsHtml() const;
    QString formatHistoryArtifactItemHtml(const WorkflowArtifactInfo &item) const;
    QString formatHistoryToolCallsHtml() const;
    QString formatHistoryToolCallItemHtml(const WorkflowToolCallInfo &item) const;
    QString formatHistoryUpdatesHtml() const;
    QString formatHistoryUpdateItemHtml(const WorkflowTaskUpdateInfo &item) const;
    QString formatTaskRetrospectiveHtml(const QJsonObject &payload, bool compact = false) const;
    QString historyUpdateTypeText(const QString &type) const;
    QJsonObject historyVerificationFromStepOutput(const QJsonObject &output) const;
    QJsonObject historyVerificationFromToolResult(const QJsonObject &result) const;
    QJsonObject historyVerificationFromUpdatePayload(const QJsonObject &payload) const;
    QJsonObject historyVerificationForArtifact(const WorkflowArtifactInfo &artifact) const;
    QJsonObject historyDocumentContextFromStepOutput(const QJsonObject &output) const;
    QJsonObject historyDocumentContextFromToolResult(const QJsonObject &result) const;
    QJsonObject historyDocumentContextFromUpdatePayload(const QJsonObject &payload) const;
    QJsonObject historyDocumentContextForArtifact(const WorkflowArtifactInfo &artifact) const;
    QString formatHistoryVerificationHtml(const QJsonObject &verification) const;
    QString formatHistoryVerificationText(const QJsonObject &verification) const;
    QString formatHistoryDocumentContextHtml(const QJsonObject &context, int displayLimit = 2) const;
    QString formatHistoryDocumentContextText(const QJsonObject &context, int displayLimit = 3) const;
    QString formatHistoryPlanSummaryHtml() const;
    QString formatJsonPreview(const QJsonObject &object, int maxLength = 180) const;
    QString formatWorkspaceSearchResultHtml(const QJsonObject &result, int displayLimit = 3) const;
    QString formatWorkspaceSearchResultFromStepOutputHtml(const QJsonObject &output, int displayLimit = 3) const;
    QString formatWorkspaceSearchResultFromUpdatePayloadHtml(const QJsonObject &payload, int displayLimit = 2) const;
    QString formatHistoryStepsHtml() const;
    QString formatHistoryStepItemHtml(const WorkflowStepRunInfo &item) const;
    QString historyStepStatusText(const QString &status) const;
    QString historyRuntimeBadgeObjectName(const QString &status) const;
    QString historyStatusBadgeObjectName(const QString &status) const;
    QString formatHistoryLogHtml(const TaskLogEvent &event) const;
    void appendConversationHtml(const QString &html);
    void resetProgressPanel();
    void setProgressStep(int sequence, const QString &text, const QString &badgeObjectName);

private:
    QComboBox *historyStatusFilter = nullptr;
    QComboBox *historyModeFilter = nullptr;
    QComboBox *historyRiskFilter = nullptr;
    QComboBox *historyConfirmationFilter = nullptr;
    QPushButton *historyRefreshButton = nullptr;
    QTimer *historyRefreshTimer = nullptr;
    QPushButton *historyExecuteButton = nullptr;
    QPushButton *historyPauseButton = nullptr;
    QPushButton *historyCancelButton = nullptr;
    QPushButton *historyRetryButton = nullptr;
    QPushButton *historyRestoreDocumentButton = nullptr;
    QPushButton *historyMemoryButton = nullptr;
    QToolButton *historyModelRoutesButton = nullptr;
    QWidget *historyArtifactStrip = nullptr;
    QComboBox *historyArtifactCombo = nullptr;
    QToolButton *historyArtifactPreviewButton = nullptr;
    QToolButton *historyArtifactOpenButton = nullptr;
    QToolButton *historyArtifactCopyButton = nullptr;
    QPushButton *historyPrevButton = nullptr;
    QPushButton *historyNextButton = nullptr;
    QTableWidget *historyTable = nullptr;
    QTextEdit *historyDetailText = nullptr;
    QWidget *historyConfirmationSection = nullptr;
    QWidget *historyConfirmationBody = nullptr;
    QTextEdit *historyConfirmationText = nullptr;
    QLabel *historyCountLabel = nullptr;
    QLabel *historyPageLabel = nullptr;
    QLabel *historySelectionTitle = nullptr;
    QLabel *historySelectionMeta = nullptr;
    QLabel *historySelectionBadge = nullptr;
    QLabel *historyRuntimeBadge = nullptr;
    QLabel *historyRuntimeMeta = nullptr;
    QLabel *historyConfirmationIcon = nullptr;
    QLabel *historyConfirmationMeta = nullptr;
    QLabel *historyConfirmationBadge = nullptr;
    QToolButton *historyConfirmationToggleButton = nullptr;
    QPushButton *historyConfirmButton = nullptr;
    QLabel *modelSummaryLabel = nullptr;
    QLabel *modelCurrentProviderBadge = nullptr;
    QLabel *modelCurrentTransportBadge = nullptr;
    QLabel *modelCurrentModelBadge = nullptr;
    QLabel *modelCurrentKeyBadge = nullptr;
    QLabel *modelCountLabel = nullptr;
    QLabel *modelDetailBadge = nullptr;
    QLabel *modelHintLabel = nullptr;
    QLabel *modelConfigStatusBadge = nullptr;
    QLabel *modelConfigProviderLabel = nullptr;
    QLabel *modelConfigStatusLabel = nullptr;
    QLabel *codeCommandPolicyBadge = nullptr;
    QLineEdit *codeCommandPolicyInput = nullptr;
    QPushButton *codeCommandPolicyCheckButton = nullptr;
    QTextEdit *codeCommandPolicyResultText = nullptr;
    QLabel *settingsRuntimeStatusBadge = nullptr;
    QComboBox *settingsPermissionPolicyCombo = nullptr;
    QComboBox *settingsPersonalityCombo = nullptr;
    QCheckBox *settingsMemoryEnabledCheck = nullptr;
    QPushButton *settingsRefreshPreferencesButton = nullptr;
    QPushButton *settingsManageMemoriesButton = nullptr;
    QPushButton *settingsSavePreferencesButton = nullptr;
    QTextEdit *settingsRuntimeNotesText = nullptr;
    // 长期记忆管理是设置页的详情工具：用可关闭独立窗口承载表格与编辑器，避免设置首屏
    // 被低频的审计/删除控件挤满。所有记录仍需经后端安全筛查和明确确认。
    QPointer<QDialog> longTermMemoryDialog;
    QPointer<QTableWidget> longTermMemoryTable;
    QPointer<QComboBox> longTermMemoryKindCombo;
    QPointer<QLineEdit> longTermMemoryTitleInput;
    QPointer<QPlainTextEdit> longTermMemorySummaryInput;
    QPointer<QLineEdit> longTermMemoryTagsInput;
    QPointer<QCheckBox> longTermMemoryEnabledCheck;
    QPointer<QLabel> longTermMemoryStatusLabel;
    QPointer<QPushButton> longTermMemorySaveButton;
    QPointer<QPushButton> longTermMemoryDeleteButton;
    QString currentLongTermMemoryId;
    QList<LongTermMemoryInfo> currentLongTermMemories;
    bool longTermMemoryLoading = false;
    // 任务后记忆候选在独立小窗中确认，避免将一次性审计表单塞进历史详情或设置页。
    QPointer<QDialog> historyMemoryProposalDialog;
    QPointer<QComboBox> historyMemoryProposalKindCombo;
    QPointer<QLineEdit> historyMemoryProposalScopeInput;
    QPointer<QLineEdit> historyMemoryProposalTitleInput;
    QPointer<QPlainTextEdit> historyMemoryProposalSummaryInput;
    QPointer<QLineEdit> historyMemoryProposalTagsInput;
    QPointer<QLabel> historyMemoryProposalStatusLabel;
    QPointer<QPushButton> historyMemoryProposalConfirmButton;
    TaskMemoryProposalInfo activeHistoryMemoryProposal;
    QString activeDocumentAgentTaskId;
    // PDF 整理使用独立、可关闭的工作区，避免把文件操作、长结果和来源审计挤进同一页面。
    // QPointer 会在用户关闭窗口后自动置空，后台任务仍按标准任务链继续并可在历史页查看。
    QPointer<QDialog> pdfProcessingDialog;
    QPointer<QListWidget> pdfProcessingDocumentList;
    QPointer<QComboBox> pdfProcessingOperationCombo;
    QPointer<QLineEdit> pdfProcessingPageRangeInput;
    QPointer<QComboBox> pdfProcessingRotationCombo;
    QPointer<QLabel> pdfProcessingScopeLabel;
    QPointer<QLabel> pdfProcessingStatusLabel;
    QPointer<QLabel> pdfProcessingResultLabel;
    QPointer<QPushButton> pdfProcessingRunButton;
    QPointer<QPushButton> pdfProcessingRefreshButton;
    QPointer<QPushButton> pdfProcessingImportButton;
    QPointer<QPushButton> pdfProcessingOpenArtifactButton;
    QString activePdfProcessingTaskId;
    bool pdfProcessingRunning = false;
    bool pdfProcessingWorkspaceLoading = false;
    WorkflowArtifactInfo currentPdfProcessingArtifact;
    // 详情页保存只允许引用本次已经完成、来源已校验的草稿结果，不能从富文本 HTML 反向解析。
    QString currentDocumentResultTaskId;
    QJsonObject currentDocumentResultContext;
    bool documentAnalysisDetailAvailable = false;
    // 主工作台只显示当前材料的最近一份审查报告。完整报告仍由任务历史持久化，内存快照
    // 仅用于避免用户关闭弹窗后被“查看详情”误带到旧的通用分析结果。
    enum class DocumentWorkbenchReviewKind {
        None,
        Project,
        Paper
    };
    DocumentWorkbenchReviewKind latestDocumentReviewKind = DocumentWorkbenchReviewKind::None;
    QString latestDocumentReviewReference;
    ProjectReviewResult latestProjectReviewResult;
    PaperReviewResult latestPaperReviewResult;
    bool documentDraftSaving = false;
    bool documentDraftSaved = false;
    // 演示文稿只从当前已核验草稿生成。预览、确认导出和失败恢复各自有状态，避免用户双击
    // 创建多份文件，也避免把旧 planId 误用于新草稿。
    bool documentPresentationPreviewLoading = false;
    bool documentPresentationExporting = false;
    // 主工作台的“制作项目方案 PPT”会先建立同一份材料的可核验草稿；该标记只影响本次
    // 异步任务完成后的下一步引导，不改变通用 Document Agent 的结果契约。
    bool documentPresentationDraftRequested = false;
    QPointer<QDialog> documentPresentationDialog;
    QString documentPresentationTaskId;
    QString documentPresentationPlanId;
    // 项目审查只读原材料并生成独立任务；加载态避免用户连续点击产生重复检查与两份相同报告。
    bool projectDocumentReviewLoading = false;
    QString activeProjectDocumentReviewTaskId;
    QPointer<QDialog> projectDocumentReviewDialog;
    bool paperReviewLoading = false;
    QString activePaperReviewTaskId;
    QPointer<QDialog> paperReviewDialog;
    // 派生章节任务失败时保留原草稿详情，不能让一次续写失败清空用户已审阅的结果。
    bool documentSectionDraftRunning = false;
    bool documentSectionReviewRunning = false;
    bool documentSectionRevisionRunning = false;
    bool documentManualRevisionRunning = false;
    // 模板交付只重组已核验快照；独立记录运行态，避免 UI 把它误显示成模型重新分析或文件导出。
    bool documentDraftTemplateRunning = false;
    // 合并计划与创建任务分开：前者是只读查询，后者才会建立独立版本，二者均不能覆盖旧快照。
    bool documentDraftMergeLoading = false;
    bool documentDraftMergeRunning = false;
    bool documentDraftReviewRunning = false;
    // 恢复始终创建新任务；这个状态只用于让详情/历史页显示真实进度，不能代表旧稿已被改写。
    bool documentDraftRestoreRunning = false;
    // 版本差异只是一次只读查询；单独记录加载态，避免用户连点造成重复 HTTP 请求或弹出多窗。
    bool documentDraftParentDiffLoading = false;
    // “生成修订预览”只在当前详情确实来自本章审校且仍有候选建议时启用，避免菜单入口
    // 看起来可点、实际又返回 4xx 的困惑体验。
    QAction *documentSectionRevisionAction = nullptr;
    QAction *documentSectionBatchRevisionAction = nullptr;
    QAction *documentSectionManualRevisionAction = nullptr;
    QAction *documentDraftTemplateAction = nullptr;
    QAction *documentDraftMergeAction = nullptr;
    QAction *documentDraftRestoreAction = nullptr;
    QAction *documentDraftParentDiffAction = nullptr;
    QAction *documentPaperReviewAction = nullptr;
    // 最近一次保存只用于生成“另存为”的建议文件名；实际冲突和目录边界始终由后端裁决。
    QString lastSavedDocumentDraftFilename;
    QLineEdit *modelSearchInput = nullptr;
    QLineEdit *modelConfigBaseUrlInput = nullptr;
    QLineEdit *modelConfigModelInput = nullptr;
    QLineEdit *modelConfigApiKeyInput = nullptr;
    QComboBox *modelConfigThinkingCombo = nullptr;
    QPushButton *modelRefreshButton = nullptr;
    QPushButton *modelRoutesButton = nullptr;
    QPushButton *modelTestConfigButton = nullptr;
    QPushButton *modelSaveConfigButton = nullptr;
    QPushButton *modelClearKeyButton = nullptr;
    QTableWidget *modelProviderTable = nullptr;
    QTextEdit *modelDetailText = nullptr;
    // 仅缓存模型路由的脱敏展示快照，供各工作台展示“当前本次模型”提示；不保存 Key、材料或响应。
    QHash<QString, ModelRouteInfo> currentModelRoutesById;
    // 低频任务模型设置位于独立检查器；QPointer 防止网络回调写入用户已关闭的窗口。
    QPointer<ModelRouteDialog> modelRouteDialog;
    // 已导入材料的选择放在独立对话框，避免 Composer 被三个长期下拉框挤压。
    QPointer<DispatchMaterialDialog> dispatchMaterialDialog;
    // 调度台的 updates 刷新是低频单次轮询，不是长连接；这里复用一个单次定时器防抖。
    QTimer *dispatchUpdateRefreshTimer = nullptr;
    // 调度台状态点与知识库等页面共用同一真实活动指示器：运行时才旋转，终态立即停止。
    TaskActivityIndicator *dispatchActivityIndicator = nullptr;
    // 两个专业工作台的状态点由既有运行态字段驱动；共享定时器仅在其中至少一项运行时存在，
    // 用来收束异步回调中的终态，避免遗漏某条失败路径后继续显示“进行中”。
    QTimer *workbenchActivityStateTimer = nullptr;
    TaskActivityIndicator *documentActivityIndicator = nullptr;
    TaskActivityIndicator *dataActivityIndicator = nullptr;
    // 当前调度台对应的任务，用来把 WebSocket 日志和 updates 聚合结果对准同一个上下文。
    QString currentDispatchTaskId;
    // 同一任务的结果卡请求只允许有一个在途请求；任务切换或 Runtime 接管后会清空状态。
    QString currentDispatchDeliveryCardTaskId;
    bool currentDispatchDeliveryCardRequestInFlight = false;
    bool currentDispatchDeliveryCardTerminal = false;
    QString currentDispatchDeliveryOpenArtifactId;
    QString currentDispatchDeliveryOpenArtifactTaskId;
    QString currentDispatchDeliveryPreviewArtifactId;
    QString currentDispatchDeliveryPreviewArtifactTaskId;
    bool currentDispatchDeliveryOpenInProgress = false;
    QPixmap currentDispatchDeliveryImage;
    // 规划阶段返回的步骤数，方便后续把右侧 5 步进度解释成“已生成多少步骤”。
    int currentDispatchPlannedStepCount = 0;
    // 用于真实执行前的二次确认：只保存后端返回的脱敏计划摘要和步骤，不保存文件正文或绝对路径。
    WorkflowPlanSummaryInfo currentDispatchPlanSummary;
    QList<WorkflowStepInfo> currentDispatchPlanSteps;
    // 计划版本是低频详情窗状态，采用 QPointer 避免窗口关闭后异步 HTTP 回调写入悬空控件。
    QPointer<QDialog> dispatchPlanDialog;
    QPointer<QTableWidget> dispatchPlanVersionTable;
    QPointer<QTextBrowser> dispatchPlanPreview;
    QPointer<QPlainTextEdit> dispatchPlanGoalInput;
    QPointer<QLineEdit> dispatchPlanChangeSummaryInput;
    QPointer<QPushButton> dispatchPlanRevisionButton;
    // 完整会话归档为低频只读阅读窗；正文按页请求，主调度台仅保留当前会话的近轮内容。
    QPointer<QDialog> dispatchConversationArchiveDialog;
    QPointer<QTextBrowser> dispatchConversationArchiveText;
    QPointer<QLabel> dispatchPlanStatusLabel;
    QList<WorkflowPlanVersionInfo> currentDispatchPlanVersions;
    bool dispatchPlanVersionsLoading = false;
    bool dispatchPlanRevisionSubmitting = false;
    // 已经消费到的 updates 序号上限，避免刷新时重复把旧事件再写一遍。
    int currentDispatchUpdateWatermark = 0;
    QList<WorkflowTaskUpdateInfo> currentDispatchUpdates;
    bool currentDispatchNeedsClarification = false;
    // 旧版数据工作台仅能引导交接。D5.4 之后这个标记仍用于兼容历史 dry-run；新的单份
    // 数据委派会进入真实只读子任务，不会落到该分支。
    bool currentDispatchGuidedHandoff = false;
    // “从主题直接制作 PPT”是 UI 引导，不是文档读取或 Runtime 执行；单独保存，避免它
    // 被旧的数据工作台引导按钮与状态机混淆。
    bool currentDispatchPresentationHandoff = false;
    bool currentDispatchPresentationRunning = false;
    bool currentDispatchPresentationCompleted = false;
    bool currentDispatchExecutionInProgress = false;
    bool currentDispatchExecutionSubmitted = false;
    // 直接问答仍完整记录 dry-run、Runtime、子任务与来源，但聊天区只消费已验证的最终回答。
    bool currentDispatchDirectKnowledgeAnswer = false;
    // 数据预览和知识库问答共用“自动执行只读任务”的交互，但各自的最终交付协议不同：
    // 知识库需回读 K3 Evidence Gate，数据任务只读取已脱敏的本地聚合结论。
    bool currentDispatchDirectDataAnalysis = false;
    // 图表写入不是自动只读动作：客户在会话中明确确认后才进入 Runtime。完成后仍应在聊天
    // 中即时交付结果，而不是要求用户切回数据工作台或翻找任务历史。
    bool currentDispatchDataChartDelivery = false;
    // Excel 导出同样由客户自然语言确认后才进入 Runtime；完成后在当前会话立即展示回读摘要，
    // 不再让用户切回数据工作台确认文件是否生成。
    bool currentDispatchDataWorkbookDelivery = false;
    bool currentDispatchAutoExecutePending = false;
    bool currentDispatchKnowledgeAnswerResultRequested = false;
    bool currentDispatchKnowledgeAnswerDelivered = false;
    bool currentDispatchDataAnalysisDelivered = false;
    bool currentDispatchDataChartDeliveryDelivered = false;
    bool currentDispatchDataWorkbookDeliveryDelivered = false;
    // 只读知识库问答自动执行时，失败也要成为明确终态；不能让客户一直看到“正在检索”。
    bool currentDispatchKnowledgeAnswerFailed = false;
    bool currentDispatchDataAnalysisFailed = false;
    bool currentDispatchDataChartDeliveryFailed = false;
    bool currentDispatchDataWorkbookDeliveryFailed = false;
    // 终态结果卡在独立的非模态窗口中展示，允许客户拖动到不遮挡对话的位置；内嵌卡仍
    // 保留为紧凑摘要和无窗口场景的回退入口。
    QPointer<QDialog> dispatchDeliveryDialog;
    QPointer<QTextBrowser> dispatchDeliveryDialogText;
    QPointer<QLabel> dispatchDeliveryDialogImage;
    QPointer<QLabel> dispatchDeliveryDialogStatus;
    QPointer<QPushButton> dispatchDeliveryDialogOpenButton;
    QPointer<QPushButton> dispatchDeliveryDialogHistoryButton;
    QPointer<PresentationStudioDialog> dispatchPresentationDialog;
    QString currentDispatchKnowledgeAnswerChildTaskId;
    // 暂存请求同时冻结本轮显式路由偏好，避免后端启动期间客户编辑输入后改变已排队任务。
    QJsonArray pendingDispatchAgentHints;
    // workspace 文档导入入口被调度台和文档助手复用；只记录发起页，避免回调写错页面。
    QString pendingWorkspaceImportTarget;
    // 知识库导入始终先进入受控 workspace，再由后端按稳定 ID 复制到资料库私有目录。
    // 该 ID 在异步文件选择完成后仍能把材料归属到用户起初选择的资料库，而不是当前列表选中项。
    QString pendingKnowledgeImportBaseId;
    QString activeKnowledgeBaseId;
    QString activeKnowledgeIndexJobId;
    QList<KnowledgeBaseInfo> currentKnowledgeBases;
    QList<KnowledgeDocumentInfo> currentKnowledgeDocuments;
    KnowledgeVectorCapabilityInfo currentKnowledgeVectorCapability;
    KnowledgeOcrCapabilityInfo currentKnowledgeOcrCapability;
    KnowledgeOcrPreparationInfo currentKnowledgeOcrPreparation;
    QTimer *knowledgeIndexPollTimer = nullptr;
    QTimer *knowledgeOcrPreparationPollTimer = nullptr;
    QTimer *knowledgeDeletionPollTimer = nullptr;
    TaskActivityIndicator *knowledgeIndexActivityIndicator = nullptr;
    TaskActivityIndicator *knowledgeOcrActivityIndicator = nullptr;
    QFrame *knowledgeOcrPanel = nullptr;
    QLabel *knowledgeOcrStatus = nullptr;
    QLabel *knowledgeOcrHint = nullptr;
    QPushButton *knowledgePrepareOcrButton = nullptr;
    QString activeKnowledgeOcrPreparationId;
    bool knowledgeBasesLoading = false;
    bool knowledgeDocumentsLoading = false;
    bool knowledgeIndexStarting = false;
    bool knowledgeVectorPreparing = false;
    bool knowledgeOcrPreparing = false;
    bool knowledgeDeletionPending = false;
    // 调度台附件只保存后端确认的 workspace 相对引用。材料在当前会话内持续可见，客户可
    // 通过材料条主动移除；新建会话时清空，避免跨会话意外复用私有材料。
    QString dispatchSelectedDocumentRef;
    // 资料库委派只保存用户显式选择的稳定 ID。它不是本地路径，也不会触发列表扫描。
    QString dispatchSelectedKnowledgeBaseId;
    // 数据工作台只会把当前画像通过的一份受控相对引用带入调度台；同一会话后续追问会
    // 继续携带它。文件内容、预览行和绝对路径不会写进 Qt 状态。
    QString dispatchSelectedDatasetRef;
    // 默认全局范围；项目范围只是记忆检索隔离标识，不能被解释成用户磁盘路径或 Runtime 授权。
    QString currentDispatchProjectScope = QStringLiteral("global");
    // 会话 ID 只是后端自动短期上下文的稳定指针。正文、文件内容和材料原文仍留在后端受控
    // 存储，Qt 仅保存该不透明 ID 以便重启后延续同一段客户会话。
    QString currentDispatchConversationId;
    bool dispatchConversationHasMessages = false;
    bool dispatchConversationRestoreInProgress = false;
    QString currentDispatchUserGoal;
    // 后端启动是异步的。客户在 ready 前发送的第一条任务会以已经冻结的材料范围暂存，
    // 就绪后自动提交；它不是离线队列，也不会跨进程/重启持久化。
    QString pendingDispatchMessage;
    QJsonArray pendingDispatchMaterials;
    QString pendingDispatchProjectScope;
    bool dispatchSubmissionWaitingForBackend = false;
    QString pendingDocumentSelection;
    bool documentWorkspaceLoading = false;
    // 只有成功收到受控 workspace 清单才标记已加载；失败或后端重启后可以再次请求，避免
    // 初始化时的占位文案长期显示为“正在加载”。
    bool documentWorkspaceLoaded = false;
    // 只缓存 workspace 目录元数据，供调度台选择已有材料；正文和绝对源路径不进入 Qt 状态。
    QList<WorkspaceDocumentInfo> currentWorkspaceDocuments;
    // D1 的数据工作台和文档工作区互不复用：这几个状态只负责文件列表和本地画像，不能被
    // 文档助手任务或未来模型调用改变。
    QString pendingDataDatasetSelection;
    QList<DataDatasetInfo> currentDataDatasets;
    // 材料选择器的目录同步独立于工作台页面。三项都结束前只展示真实“同步中”，任何一项
    // 失败都保留可重试原因，不能让最后返回的成功响应掩盖先前失败。
    bool dispatchMaterialDocumentsPending = false;
    bool dispatchMaterialKnowledgePending = false;
    bool dispatchMaterialDatasetsPending = false;
    QString dispatchMaterialCatalogError;
    // 数据导入既可从数据工作台发起，也可由调度台“添加材料”发起。回调必须知道回执属于
    // 哪个客户入口，避免调度台导入完成后反而跳转或重置数据工作台的当前界面。
    QString pendingDataDatasetImportTarget;
    bool dataWorkspaceLoading = false;
    bool dataWorkspaceLoaded = false;
    bool dataProfileLoading = false;
    bool dataProfileReady = false;
    // 画像请求与结果都绑定到当前受控文件名；404 只自动刷新一次，避免异常网络状态反复循环。
    QString activeDataProfileDataset;
    bool dataProfileRefreshRecoveryAttempted = false;
    // 推荐请求独立于 D2 聚合预览：客户等待建议时仍可直接输入自己的问题开始分析。
    bool dataRecommendationLoading = false;
    bool dataAnalysisLoading = false;
    bool dataWorkbookExportLoading = false;
    bool dataChartExportLoading = false;
    bool dataTransformationPreviewLoading = false;
    bool dataTransformationExportLoading = false;
    // 导出任务拥有独立 ID，避免其它页面的 WebSocket 事件误更新数据工作台状态。
    QString activeDataWorkbookExportTaskId;
    QString activeDataChartExportTaskId;
    QString activeDataTransformationTaskId;
    // 当前数据会话最近一次导出的任务 ID。任务结束后仍可跳转到统一历史页查看受控交付物。
    QString lastDataWorkbookExportTaskId;
    QString lastDataChartExportTaskId;
    QString lastDataTransformationTaskId;
    QPointer<QDialog> dataChartDashboardDialog;
    QPointer<QListWidget> dataChartDashboardList;
    QPointer<QScrollArea> dataChartDashboardScrollArea;
    QPointer<QLabel> dataChartDashboardImageLabel;
    QPointer<QLabel> dataChartDashboardStatusLabel;
    QString dataChartDashboardTaskId;
    QHash<QString, QPixmap> dataChartDashboardPixmapCache;
    QJsonObject lastDataAnalysisPreview;
    QJsonObject pendingDataTransformationRequest;
    QJsonObject lastDataTransformationPreview;
    bool documentAgentRunning = false;
    // 调度台只保留当前 runtime 的轻量 UI 状态，真正的执行记录仍以历史页/后端为准。
    QString currentDispatchRuntimeMode;
    QString currentDispatchRuntimeStatus;
    bool currentDispatchHasPendingPermission = false;
    int currentDispatchArtifactCount = 0;
    bool codeCommandPolicyCheckInProgress = false;
    bool runtimePreferencesLoading = false;
    bool runtimePreferencesSaving = false;
    QString currentHistoryTaskId;
    QString currentHistorySummary;
    QString currentHistoryStatus;
    QString currentHistoryMode;
    QString currentHistoryRiskLevel;
    QString currentHistoryUpdatedAt;
    bool currentHistoryRequiresConfirmation = false;
    QString currentHistoryArtifactId;
    QString pendingHistoryArtifactPreviewTaskId;
    QString pendingHistoryArtifactPreviewId;
    QString pendingHistoryArtifactOpenTaskId;
    QString pendingHistoryArtifactOpenId;
    // 导出完成后只从同任务的受控 artifact 清单中取 ID，再请求后端打开，避免 UI 猜测文件路径。
    QString pendingAutoOpenArtifactTaskId;
    int currentHistoryStepCount = 0;
    WorkflowPlanSummaryInfo currentHistoryPlanSummary;
    QList<WorkflowStepInfo> currentHistoryPlanSteps;
    QList<WorkflowStepRunInfo> currentHistorySteps;
    QList<TaskLogEvent> currentHistoryEvents;
    QList<RuntimePermissionItem> currentHistoryPermissions;
    WorkflowRuntimeStateInfo currentHistoryRuntimeState;
    WorkflowRuntimeMetricsResult currentHistoryMetrics;
    QList<WorkflowModelRouteAuditInfo> currentHistoryModelRoutes;
    WorkflowTaskEvaluationResult currentHistoryEvaluation;
    QHash<QString, WorkflowNodeContractInfo> workflowNodeContractsByStep;
    QHash<QString, WorkflowNodeContractInfo> workflowNodeContractsByTool;
    QList<WorkflowArtifactInfo> currentHistoryArtifacts;
    QList<WorkflowToolCallInfo> currentHistoryToolCalls;
    QList<WorkflowTaskUpdateInfo> currentHistoryUpdates;
    QStringList pendingPermissionApprovalQueue;
    bool historyPermissionApprovalInProgress = false;
    bool historyArtifactPreviewInProgress = false;
    bool historyArtifactOpenInProgress = false;
    int historyLimit = 20;
    int historyOffset = 0;
    int historyTotal = 0;
    QString pendingHistoryFocusTaskId;
    bool currentHistoryConfirmationAcknowledged = false;
    bool currentHistoryConfirmationExpanded = false;
    bool currentHistoryPlanLoaded = false;
    bool currentHistoryStepsLoaded = false;
    bool currentHistoryLogsLoaded = false;
    bool currentHistoryPermissionsLoaded = false;
    bool currentHistoryRuntimeStateLoaded = false;
    bool currentHistoryMetricsLoaded = false;
    bool currentHistoryModelRoutesLoaded = false;
    bool currentHistoryEvaluationLoaded = false;
    bool currentHistoryArtifactsLoaded = false;
    bool currentHistoryToolCallsLoaded = false;
    bool currentHistoryUpdatesLoaded = false;
    QString currentHistoryPlanError;
    QString currentHistoryStepsError;
    QString currentHistoryLogsError;
    QString currentHistoryPermissionsError;
    QString currentHistoryRuntimeStateError;
    QString currentHistoryMetricsError;
    QString currentHistoryModelRoutesError;
    QString currentHistoryEvaluationError;
    bool workflowNodeContractsLoaded = false;
    QString workflowNodeContractsError;
    QString currentHistoryArtifactsError;
    QString currentHistoryToolCallsError;
    QString currentHistoryUpdatesError;
    ModelProviderStatus currentModelStatus;
    QList<ModelProviderInfo> currentModelProviders;
    bool modelProvidersLoading = false;
    bool modelConfigSaving = false;
    bool modelConnectionTesting = false;
};

#endif // MAINWINDOW_H
