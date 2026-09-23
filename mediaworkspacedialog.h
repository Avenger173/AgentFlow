#ifndef MEDIAWORKSPACEDIALOG_H
#define MEDIAWORKSPACEDIALOG_H

#include <QDialog>
#include <QList>
#include <QRect>

#include "backendclient.h"

class QComboBox;
class QLabel;
class QListWidget;
class MediaImageCanvas;
class QPlainTextEdit;
class QPushButton;
class QSpinBox;
class QTabWidget;
class QToolButton;

// 图片工作区只展示后端受控项目中的副本与修订；本地原图路径不会传给后端或写入项目元数据。
class MediaWorkspaceDialog : public QDialog
{
public:
    explicit MediaWorkspaceDialog(BackendClient *backendClient, QWidget *parent = nullptr);

private:
    void buildUi();
    void connectBackend();
    void refreshProjects();
    void createProject();
    void selectProject(int index);
    void importImage();
    void selectAsset();
    void selectRevision();
    void createRevision(const QString &operation, const QJsonObject &parameters = {});
    void startAiImageEdit();
    void navigateHistory(const QString &action);
    void exportSelectedRevision();
    void populateProjects();
    void populateAssets();
    void populateLayerSources();
    void populateLayerStack();
    void populateRevisions();
    void showPreview(const QByteArray &content);
    void clearPreview(const QString &message);
    void configureGeometryEditors(int width, int height);
    void updateCropEditorRanges();
    void updateMaskEditorRanges();
    void updateLayerEditorRanges();
    void updateResizeEditorRanges();
    void updateCanvasSelectionMode(int tabIndex);
    void syncCanvasSelection();
    void applyCanvasSelection(const QRect &selection);
    void updateActionState();
    void applyLayerStack();
    void setStatus(const QString &message, bool isError = false);
    QString revisionLabel(const MediaImageRevisionInfo &revision) const;
    QString currentProjectId() const;

    BackendClient *backendClient;
    QList<MediaProjectInfo> projects;
    MediaProjectDetailResult projectDetail;
    MediaImageAssetInfo currentAsset;
    QList<MediaImageRevisionInfo> revisions;
    MediaImageLayerStackResult layerStack;
    QString activeProjectId;
    QString activeAssetId;
    QString selectedRevisionId;
    QString preferredRevisionId;
    QString pendingRevisionId;
    QString pendingRevisionTaskId;
    QString pendingHistoryAction;
    QString pendingImportProjectId;
    QString pendingSavePath;
    QString pendingExportId;
    QString pendingExportTaskId;
    bool imageImportPending = false;
    bool revisionRequestPending = false;
    bool pendingAiEdit = false;
    bool historyNavigationPending = false;
    bool revisionConflictRefreshPending = false;
    bool layerStackRequestPending = false;
    int selectedRevisionWidth = 0;
    int selectedRevisionHeight = 0;

    QComboBox *projectCombo = nullptr;
    QToolButton *newProjectButton = nullptr;
    QToolButton *refreshButton = nullptr;
    QPushButton *importButton = nullptr;
    QListWidget *assetList = nullptr;
    QListWidget *revisionList = nullptr;
    MediaImageCanvas *previewCanvas = nullptr;
    QLabel *previewMetaLabel = nullptr;
    QLabel *statusLabel = nullptr;
    QToolButton *undoButton = nullptr;
    QToolButton *redoButton = nullptr;
    QPushButton *rotateLeftButton = nullptr;
    QPushButton *rotateRightButton = nullptr;
    QPushButton *flipButton = nullptr;
    QPushButton *grayscaleButton = nullptr;
    QPlainTextEdit *aiInstructionEdit = nullptr;
    QPushButton *aiEditButton = nullptr;
    QSpinBox *brightnessSpin = nullptr;
    QSpinBox *contrastSpin = nullptr;
    QSpinBox *saturationSpin = nullptr;
    QPushButton *colorApplyButton = nullptr;
    QSpinBox *cropXSpin = nullptr;
    QSpinBox *cropYSpin = nullptr;
    QSpinBox *cropWidthSpin = nullptr;
    QSpinBox *cropHeightSpin = nullptr;
    QPushButton *cropApplyButton = nullptr;
    QSpinBox *maskXSpin = nullptr;
    QSpinBox *maskYSpin = nullptr;
    QSpinBox *maskWidthSpin = nullptr;
    QSpinBox *maskHeightSpin = nullptr;
    QPushButton *maskApplyButton = nullptr;
    QComboBox *layerSourceCombo = nullptr;
    QListWidget *layerList = nullptr;
    QToolButton *layerUpButton = nullptr;
    QToolButton *layerDownButton = nullptr;
    QSpinBox *layerXSpin = nullptr;
    QSpinBox *layerYSpin = nullptr;
    QSpinBox *layerOpacitySpin = nullptr;
    QPushButton *layerApplyButton = nullptr;
    QSpinBox *resizeWidthSpin = nullptr;
    QSpinBox *resizeHeightSpin = nullptr;
    QPushButton *resizeApplyButton = nullptr;
    QPushButton *exportButton = nullptr;
    QTabWidget *editTabs = nullptr;
    int cropTabIndex = -1;
    int maskTabIndex = -1;
};

#endif // MEDIAWORKSPACEDIALOG_H
