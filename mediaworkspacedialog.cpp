#include "mediaworkspacedialog.h"
#include "mediaimagecanvas.h"
#include "spinboxarrowstyle.h"

#include <algorithm>

#include <QAbstractItemView>
#include <QComboBox>
#include <QDateTime>
#include <QDialogButtonBox>
#include <QFile>
#include <QFileDialog>
#include <QFileInfo>
#include <QFrame>
#include <QFont>
#include <QGridLayout>
#include <QGuiApplication>
#include <QHBoxLayout>
#include <QJsonArray>
#include <QLabel>
#include <QLineEdit>
#include <QListWidget>
#include <QListWidgetItem>
#include <QMessageBox>
#include <QPlainTextEdit>
#include <QPixmap>
#include <QPushButton>
#include <QSaveFile>
#include <QScreen>
#include <QScrollArea>
#include <QSignalBlocker>
#include <QSizePolicy>
#include <QSpinBox>
#include <QSplitter>
#include <QStyle>
#include <QTabWidget>
#include <QTimer>
#include <QToolButton>
#include <QVBoxLayout>

namespace {

constexpr qint64 MaxImportBytes = 20LL * 1024 * 1024;

bool forceQtFileDialogsForTesting()
{
    return qEnvironmentVariableIntValue("AGENTFLOW_TEST_FORCE_QT_FILE_DIALOGS") == 1;
}

QString selectImageForImport(QWidget *parent)
{
    QFileDialog dialog(parent,
                       QStringLiteral("导入图片"),
                       QString(),
                       QStringLiteral("图片文件 (*.png *.jpg *.jpeg *.webp)"));
    dialog.setFileMode(QFileDialog::ExistingFile);
    dialog.setOption(QFileDialog::DontUseNativeDialog, forceQtFileDialogsForTesting());
    if (dialog.exec() != QDialog::Accepted) {
        return {};
    }
    return dialog.selectedFiles().value(0);
}

QString selectPngExportPath(QWidget *parent, const QString &suggestedName)
{
    QFileDialog dialog(parent,
                       QStringLiteral("导出 PNG"),
                       suggestedName,
                       QStringLiteral("PNG 图片 (*.png)"));
    dialog.setAcceptMode(QFileDialog::AcceptSave);
    dialog.setFileMode(QFileDialog::AnyFile);
    dialog.selectFile(suggestedName);
    dialog.setOption(QFileDialog::DontUseNativeDialog, forceQtFileDialogsForTesting());
    if (dialog.exec() != QDialog::Accepted) {
        return {};
    }
    return dialog.selectedFiles().value(0);
}

QString operationDisplayName(const QString &operation)
{
    if (operation == QStringLiteral("import")) {
        return QStringLiteral("导入原图");
    }
    if (operation == QStringLiteral("rotate_left")) {
        return QStringLiteral("向左旋转");
    }
    if (operation == QStringLiteral("rotate_right")) {
        return QStringLiteral("向右旋转");
    }
    if (operation == QStringLiteral("flip_horizontal")) {
        return QStringLiteral("水平镜像");
    }
    if (operation == QStringLiteral("grayscale")) {
        return QStringLiteral("灰度处理");
    }
    if (operation == QStringLiteral("adjust_color")) {
        return QStringLiteral("色彩调整");
    }
    if (operation == QStringLiteral("crop")) {
        return QStringLiteral("裁剪");
    }
    if (operation == QStringLiteral("resize")) {
        return QStringLiteral("缩放");
    }
    if (operation == QStringLiteral("apply_rect_mask")) {
        return QStringLiteral("矩形蒙版");
    }
    if (operation == QStringLiteral("composite_raster_layer")) {
        return QStringLiteral("叠加图层");
    }
    if (operation == QStringLiteral("recompose_raster_layers")) {
        return QStringLiteral("重组图层");
    }
    if (operation == QStringLiteral("ai_image_edit")) {
        return QStringLiteral("AI 修图");
    }
    return operation;
}

QString imageSizeText(int width, int height)
{
    return QStringLiteral("%1 x %2").arg(width).arg(height);
}

QString signedPercent(int value)
{
    return QStringLiteral("%1%2%")
        .arg(value >= 0 ? QStringLiteral("+") : QString())
        .arg(value);
}

QString revisionParameterSummary(const MediaImageRevisionInfo &revision)
{
    const QJsonObject &parameters = revision.parameters;
    if (revision.operation == QStringLiteral("adjust_color")) {
        return QStringLiteral("亮 %1 / 对 %2 / 饱 %3")
            .arg(signedPercent(parameters.value(QStringLiteral("brightness")).toInt()),
                 signedPercent(parameters.value(QStringLiteral("contrast")).toInt()),
                 signedPercent(parameters.value(QStringLiteral("saturation")).toInt()));
    }
    if (revision.operation == QStringLiteral("crop")) {
        return QStringLiteral("x=%1, y=%2, %3")
            .arg(parameters.value(QStringLiteral("x")).toInt())
            .arg(parameters.value(QStringLiteral("y")).toInt())
            .arg(imageSizeText(parameters.value(QStringLiteral("width")).toInt(),
                               parameters.value(QStringLiteral("height")).toInt()));
    }
    if (revision.operation == QStringLiteral("resize")) {
        return QStringLiteral("目标 %1")
            .arg(imageSizeText(parameters.value(QStringLiteral("width")).toInt(),
                               parameters.value(QStringLiteral("height")).toInt()));
    }
    if (revision.operation == QStringLiteral("apply_rect_mask")) {
        return QStringLiteral("保留 x=%1, y=%2, %3")
            .arg(parameters.value(QStringLiteral("x")).toInt())
            .arg(parameters.value(QStringLiteral("y")).toInt())
            .arg(imageSizeText(parameters.value(QStringLiteral("width")).toInt(),
                               parameters.value(QStringLiteral("height")).toInt()));
    }
    if (revision.operation == QStringLiteral("composite_raster_layer")) {
        return QStringLiteral("x=%1, y=%2, %3%")
            .arg(parameters.value(QStringLiteral("x")).toInt())
            .arg(parameters.value(QStringLiteral("y")).toInt())
            .arg(parameters.value(QStringLiteral("opacity")).toInt());
    }
    if (revision.operation == QStringLiteral("ai_image_edit")) {
        const QString instruction = parameters.value(QStringLiteral("instruction")).toString();
        return instruction.size() > 48 ? QStringLiteral("%1...").arg(instruction.left(48)) : instruction;
    }
    return {};
}

} // namespace

MediaWorkspaceDialog::MediaWorkspaceDialog(BackendClient *backendClient, QWidget *parent)
    : QDialog(parent)
    , backendClient(backendClient)
{
    buildUi();
    connectBackend();
    refreshProjects();
}

void MediaWorkspaceDialog::buildUi()
{
    setObjectName(QStringLiteral("mediaWorkspaceDialog"));
    setAccessibleName(QStringLiteral("mediaWorkspaceDialog"));
    setWindowTitle(QStringLiteral("图片工作区"));
    const QScreen *targetScreen = screen();
    if (!targetScreen) {
        targetScreen = QGuiApplication::primaryScreen();
    }
    const QSize availableSize = targetScreen ? targetScreen->availableGeometry().size()
                                             : QSize(1160, 740);
    bool hasExplicitScaleFactor = false;
    const qreal explicitScaleFactor = qEnvironmentVariable("QT_SCALE_FACTOR")
                                           .toDouble(&hasExplicitScaleFactor);
    const QSize effectiveAvailableSize = hasExplicitScaleFactor && explicitScaleFactor > 1.0
                                             ? QSize(qMax(1, static_cast<int>(availableSize.width() / explicitScaleFactor)),
                                                     qMax(1, static_cast<int>(availableSize.height() / explicitScaleFactor)))
                                             : availableSize;
    const QSize screenBound(qMax(640, effectiveAvailableSize.width() - 32),
                            qMax(480, effectiveAvailableSize.height() - 32));
    const QSize initialSize(qMin(1160, screenBound.width()),
                            qMin(740, screenBound.height()));
    const bool useCompactVerticalLayout = initialSize.height() < 650;
    setMinimumSize(qMin(980, initialSize.width()), qMin(650, initialSize.height()));
    resize(initialSize);

    auto *rootLayout = new QVBoxLayout(this);
    rootLayout->setContentsMargins(20,
                                   useCompactVerticalLayout ? 4 : 18,
                                   20,
                                   useCompactVerticalLayout ? 4 : 16);
    rootLayout->setSpacing(useCompactVerticalLayout ? 4 : 12);

    auto *headerLayout = new QHBoxLayout;
    headerLayout->setSpacing(8);
    auto *titleLabel = new QLabel(QStringLiteral("图片工作区"), this);
    QFont titleFont = titleLabel->font();
    titleFont.setPointSize(titleFont.pointSize() + 5);
    titleFont.setBold(true);
    titleLabel->setFont(titleFont);
    headerLayout->addWidget(titleLabel);
    headerLayout->addSpacing(18);
    auto *projectLabel = new QLabel(QStringLiteral("项目"), this);
    headerLayout->addWidget(projectLabel);

    projectCombo = new QComboBox(this);
    projectCombo->setMinimumWidth(270);
    projectCombo->setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Fixed);
    headerLayout->addWidget(projectCombo, 1);

    newProjectButton = new QToolButton(this);
    newProjectButton->setObjectName(QStringLiteral("mediaWorkspaceNewProjectButton"));
    newProjectButton->setIcon(style()->standardIcon(QStyle::SP_FileDialogNewFolder));
    newProjectButton->setToolTip(QStringLiteral("新建图片项目"));
    newProjectButton->setAccessibleName(QStringLiteral("新建图片项目"));
    headerLayout->addWidget(newProjectButton);

    refreshButton = new QToolButton(this);
    refreshButton->setIcon(style()->standardIcon(QStyle::SP_BrowserReload));
    refreshButton->setToolTip(QStringLiteral("刷新图片项目"));
    refreshButton->setAccessibleName(QStringLiteral("刷新图片项目"));
    headerLayout->addWidget(refreshButton);

    auto *closeButton = new QPushButton(QStringLiteral("关闭"), this);
    headerLayout->addWidget(closeButton);
    rootLayout->addLayout(headerLayout);

    auto *splitter = new QSplitter(Qt::Horizontal, this);
    splitter->setChildrenCollapsible(false);

    auto *libraryPane = new QWidget;
    auto *libraryLayout = new QVBoxLayout(libraryPane);
    libraryLayout->setContentsMargins(0, 0, 0, 0);
    libraryLayout->setSpacing(useCompactVerticalLayout ? 4 : 8);

    auto *assetHeader = new QHBoxLayout;
    auto *assetLabel = new QLabel(QStringLiteral("素材"), libraryPane);
    QFont sectionFont = assetLabel->font();
    sectionFont.setBold(true);
    assetLabel->setFont(sectionFont);
    assetHeader->addWidget(assetLabel);
    assetHeader->addStretch();
    importButton = new QPushButton(QStringLiteral("导入图片"), libraryPane);
    importButton->setObjectName(QStringLiteral("mediaWorkspaceImportButton"));
    importButton->setToolTip(QStringLiteral("导入 JPEG、PNG 或 WebP 图片"));
    assetHeader->addWidget(importButton);
    libraryLayout->addLayout(assetHeader);

    assetList = new QListWidget(libraryPane);
    assetList->setSelectionMode(QAbstractItemView::SingleSelection);
    assetList->setMinimumHeight(useCompactVerticalLayout ? 70 : 190);
    libraryLayout->addWidget(assetList, 3);

    auto *versionLabel = new QLabel(QStringLiteral("版本"), libraryPane);
    versionLabel->setFont(sectionFont);
    libraryLayout->addWidget(versionLabel);
    revisionList = new QListWidget(libraryPane);
    revisionList->setSelectionMode(QAbstractItemView::SingleSelection);
    revisionList->setMinimumHeight(useCompactVerticalLayout ? 70 : 210);
    libraryLayout->addWidget(revisionList, 4);

    auto *previewPane = new QWidget(splitter);
    auto *previewLayout = new QVBoxLayout(previewPane);
    previewLayout->setContentsMargins(0, 0, 0, 0);
    previewLayout->setSpacing(useCompactVerticalLayout ? 4 : 10);

    previewMetaLabel = new QLabel(previewPane);
    previewMetaLabel->setObjectName(QStringLiteral("subText"));
    previewMetaLabel->setWordWrap(true);
    previewLayout->addWidget(previewMetaLabel);

    auto *previewScroll = new QScrollArea(previewPane);
    previewScroll->setWidgetResizable(true);
    previewScroll->setAlignment(Qt::AlignCenter);
    previewScroll->setFrameShape(QFrame::StyledPanel);
    previewCanvas = new MediaImageCanvas(previewScroll);
    previewScroll->setWidget(previewCanvas);
    previewLayout->addWidget(previewScroll, 1);

    editTabs = new QTabWidget(previewPane);
    editTabs->setDocumentMode(true);

    auto *basicTab = new QWidget(editTabs);
    auto *basicLayout = new QHBoxLayout(basicTab);
    basicLayout->setContentsMargins(8, 8, 8, 8);
    basicLayout->setSpacing(8);
    undoButton = new QToolButton(basicTab);
    undoButton->setObjectName(QStringLiteral("mediaWorkspaceUndoButton"));
    undoButton->setIcon(style()->standardIcon(QStyle::SP_ArrowBack));
    undoButton->setToolTip(QStringLiteral("撤销最后一次图片编辑"));
    undoButton->setAccessibleName(QStringLiteral("撤销图片编辑"));
    basicLayout->addWidget(undoButton);
    redoButton = new QToolButton(basicTab);
    redoButton->setObjectName(QStringLiteral("mediaWorkspaceRedoButton"));
    redoButton->setIcon(style()->standardIcon(QStyle::SP_ArrowForward));
    redoButton->setToolTip(QStringLiteral("重做已撤销的图片编辑"));
    redoButton->setAccessibleName(QStringLiteral("重做图片编辑"));
    basicLayout->addWidget(redoButton);
    rotateLeftButton = new QPushButton(QStringLiteral("左转"), basicTab);
    rotateLeftButton->setObjectName(QStringLiteral("mediaWorkspaceRotateLeftButton"));
    rotateLeftButton->setToolTip(QStringLiteral("基于当前版本向左旋转 90 度"));
    basicLayout->addWidget(rotateLeftButton);
    rotateRightButton = new QPushButton(QStringLiteral("右转"), basicTab);
    rotateRightButton->setObjectName(QStringLiteral("mediaWorkspaceRotateRightButton"));
    rotateRightButton->setToolTip(QStringLiteral("基于当前版本向右旋转 90 度"));
    basicLayout->addWidget(rotateRightButton);
    flipButton = new QPushButton(QStringLiteral("镜像"), basicTab);
    flipButton->setToolTip(QStringLiteral("基于当前版本进行水平镜像"));
    basicLayout->addWidget(flipButton);
    grayscaleButton = new QPushButton(QStringLiteral("灰度"), basicTab);
    grayscaleButton->setToolTip(QStringLiteral("基于当前版本生成灰度版本"));
    basicLayout->addWidget(grayscaleButton);
    basicLayout->addStretch();
    exportButton = new QPushButton(QStringLiteral("导出 PNG"), basicTab);
    exportButton->setObjectName(QStringLiteral("mediaWorkspaceExportButton"));
    exportButton->setToolTip(QStringLiteral("将所选版本另存为 PNG"));
    basicLayout->addWidget(exportButton);
    editTabs->addTab(basicTab, QStringLiteral("基础"));

    auto *aiTab = new QWidget(editTabs);
    auto *aiLayout = new QVBoxLayout(aiTab);
    aiLayout->setContentsMargins(8, 8, 8, 8);
    aiLayout->setSpacing(8);
    aiInstructionEdit = new QPlainTextEdit(aiTab);
    aiInstructionEdit->setObjectName(QStringLiteral("mediaWorkspaceAiInstructionInput"));
    aiInstructionEdit->setAccessibleName(QStringLiteral("AI 修图指令"));
    aiInstructionEdit->setPlaceholderText(QStringLiteral("例如：把中央的红色圆形改成绿色，其他区域保持不变"));
    aiInstructionEdit->setMaximumHeight(72);
    aiInstructionEdit->setToolTip(QStringLiteral("基于当前版本提交一次 AI 修图，结果将生成可撤销的新版本"));
    aiLayout->addWidget(aiInstructionEdit);
    auto *aiActionLayout = new QHBoxLayout;
    aiActionLayout->addStretch();
    aiEditButton = new QPushButton(QStringLiteral("开始修图"), aiTab);
    aiEditButton->setObjectName(QStringLiteral("primaryButton"));
    aiEditButton->setAccessibleName(QStringLiteral("提交 AI 修图"));
    aiEditButton->setMinimumHeight(34);
    aiEditButton->setToolTip(QStringLiteral("调用当前配置的 AI 修图模型并回读验证结果"));
    aiActionLayout->addWidget(aiEditButton);
    aiLayout->addLayout(aiActionLayout);
    editTabs->addTab(aiTab, QStringLiteral("AI 修图"));

    auto configurePercentSpin = [this](QSpinBox *spin) {
        spin->setRange(-100, 100);
        spin->setSuffix(QStringLiteral("%"));
        spin->setButtonSymbols(QAbstractSpinBox::UpDownArrows);
        installSpinBoxArrowStyle(spin);
    };
    auto *colorTab = new QWidget(editTabs);
    auto *colorLayout = new QGridLayout(colorTab);
    colorLayout->setContentsMargins(8, 8, 8, 8);
    colorLayout->setHorizontalSpacing(10);
    colorLayout->setVerticalSpacing(6);
    colorLayout->addWidget(new QLabel(QStringLiteral("亮度"), colorTab), 0, 0);
    brightnessSpin = new QSpinBox(colorTab);
    configurePercentSpin(brightnessSpin);
    colorLayout->addWidget(brightnessSpin, 0, 1);
    colorLayout->addWidget(new QLabel(QStringLiteral("对比度"), colorTab), 1, 0);
    contrastSpin = new QSpinBox(colorTab);
    configurePercentSpin(contrastSpin);
    colorLayout->addWidget(contrastSpin, 1, 1);
    colorLayout->addWidget(new QLabel(QStringLiteral("饱和度"), colorTab), 2, 0);
    saturationSpin = new QSpinBox(colorTab);
    configurePercentSpin(saturationSpin);
    colorLayout->addWidget(saturationSpin, 2, 1);
    colorApplyButton = new QPushButton(QStringLiteral("应用调色"), colorTab);
    colorApplyButton->setToolTip(QStringLiteral("根据亮度、对比度和饱和度生成新版本"));
    colorLayout->addWidget(colorApplyButton, 3, 0, 1, 2);
    colorLayout->setColumnStretch(1, 1);
    editTabs->addTab(colorTab, QStringLiteral("调色"));

    auto configurePixelSpin = [this](QSpinBox *spin) {
        spin->setRange(0, 0);
        spin->setButtonSymbols(QAbstractSpinBox::UpDownArrows);
        installSpinBoxArrowStyle(spin);
    };
    auto *cropTab = new QWidget(editTabs);
    auto *cropLayout = new QGridLayout(cropTab);
    cropLayout->setContentsMargins(8, 8, 8, 8);
    cropLayout->setHorizontalSpacing(10);
    cropLayout->setVerticalSpacing(6);
    cropLayout->addWidget(new QLabel(QStringLiteral("X"), cropTab), 0, 0);
    cropXSpin = new QSpinBox(cropTab);
    configurePixelSpin(cropXSpin);
    cropLayout->addWidget(cropXSpin, 0, 1);
    cropLayout->addWidget(new QLabel(QStringLiteral("Y"), cropTab), 0, 2);
    cropYSpin = new QSpinBox(cropTab);
    configurePixelSpin(cropYSpin);
    cropLayout->addWidget(cropYSpin, 0, 3);
    cropLayout->addWidget(new QLabel(QStringLiteral("宽"), cropTab), 1, 0);
    cropWidthSpin = new QSpinBox(cropTab);
    configurePixelSpin(cropWidthSpin);
    cropLayout->addWidget(cropWidthSpin, 1, 1);
    cropLayout->addWidget(new QLabel(QStringLiteral("高"), cropTab), 1, 2);
    cropHeightSpin = new QSpinBox(cropTab);
    configurePixelSpin(cropHeightSpin);
    cropLayout->addWidget(cropHeightSpin, 1, 3);
    cropApplyButton = new QPushButton(QStringLiteral("应用裁剪"), cropTab);
    cropApplyButton->setToolTip(QStringLiteral("根据当前版本的像素坐标生成裁剪版本"));
    cropLayout->addWidget(cropApplyButton, 2, 0, 1, 4);
    cropLayout->setColumnStretch(1, 1);
    cropLayout->setColumnStretch(3, 1);
    cropTabIndex = editTabs->addTab(cropTab, QStringLiteral("裁剪"));

    auto *maskTab = new QWidget(editTabs);
    auto *maskLayout = new QGridLayout(maskTab);
    maskLayout->setContentsMargins(8, 8, 8, 8);
    maskLayout->setHorizontalSpacing(10);
    maskLayout->setVerticalSpacing(6);
    maskLayout->addWidget(new QLabel(QStringLiteral("X"), maskTab), 0, 0);
    maskXSpin = new QSpinBox(maskTab);
    configurePixelSpin(maskXSpin);
    maskLayout->addWidget(maskXSpin, 0, 1);
    maskLayout->addWidget(new QLabel(QStringLiteral("Y"), maskTab), 0, 2);
    maskYSpin = new QSpinBox(maskTab);
    configurePixelSpin(maskYSpin);
    maskLayout->addWidget(maskYSpin, 0, 3);
    maskLayout->addWidget(new QLabel(QStringLiteral("宽"), maskTab), 1, 0);
    maskWidthSpin = new QSpinBox(maskTab);
    configurePixelSpin(maskWidthSpin);
    maskLayout->addWidget(maskWidthSpin, 1, 1);
    maskLayout->addWidget(new QLabel(QStringLiteral("高"), maskTab), 1, 2);
    maskHeightSpin = new QSpinBox(maskTab);
    configurePixelSpin(maskHeightSpin);
    maskLayout->addWidget(maskHeightSpin, 1, 3);
    maskApplyButton = new QPushButton(QStringLiteral("应用蒙版"), maskTab);
    maskApplyButton->setToolTip(QStringLiteral("保留矩形区域的像素，并将区域外设为透明"));
    maskLayout->addWidget(maskApplyButton, 2, 0, 1, 4);
    maskLayout->setColumnStretch(1, 1);
    maskLayout->setColumnStretch(3, 1);
    maskTabIndex = editTabs->addTab(maskTab, QStringLiteral("蒙版"));

    auto *layerTab = new QWidget(editTabs);
    auto *layerLayout = new QGridLayout(layerTab);
    layerLayout->setContentsMargins(8, 8, 8, 8);
    layerLayout->setHorizontalSpacing(10);
    layerLayout->setVerticalSpacing(6);
    layerLayout->addWidget(new QLabel(QStringLiteral("素材"), layerTab), 0, 0);
    layerSourceCombo = new QComboBox(layerTab);
    layerSourceCombo->setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Fixed);
    layerLayout->addWidget(layerSourceCombo, 0, 1, 1, 3);
    layerLayout->addWidget(new QLabel(QStringLiteral("X"), layerTab), 1, 0);
    layerXSpin = new QSpinBox(layerTab);
    configurePixelSpin(layerXSpin);
    layerLayout->addWidget(layerXSpin, 1, 1);
    layerLayout->addWidget(new QLabel(QStringLiteral("Y"), layerTab), 1, 2);
    layerYSpin = new QSpinBox(layerTab);
    configurePixelSpin(layerYSpin);
    layerLayout->addWidget(layerYSpin, 1, 3);
    layerLayout->addWidget(new QLabel(QStringLiteral("不透明度"), layerTab), 2, 0);
    layerOpacitySpin = new QSpinBox(layerTab);
    layerOpacitySpin->setRange(1, 100);
    layerOpacitySpin->setValue(100);
    layerOpacitySpin->setSuffix(QStringLiteral("%"));
    layerOpacitySpin->setButtonSymbols(QAbstractSpinBox::UpDownArrows);
    installSpinBoxArrowStyle(layerOpacitySpin);
    layerLayout->addWidget(layerOpacitySpin, 2, 1);
    layerApplyButton = new QPushButton(QStringLiteral("叠加图层"), layerTab);
    layerApplyButton->setToolTip(QStringLiteral("按位置和不透明度叠加选中的项目素材，并固定来源版本"));
    layerLayout->addWidget(layerApplyButton, 3, 0, 1, 4);
    auto *stackLabel = new QLabel(QStringLiteral("当前图层栈"), layerTab);
    layerLayout->addWidget(stackLabel, 4, 0, 1, 4);
    layerList = new QListWidget(layerTab);
    layerList->setSelectionMode(QAbstractItemView::SingleSelection);
    layerList->setMinimumHeight(96);
    layerList->setMaximumHeight(132);
    layerLayout->addWidget(layerList, 5, 0, 1, 3);
    auto *layerOrderLayout = new QVBoxLayout;
    layerOrderLayout->setContentsMargins(0, 0, 0, 0);
    layerOrderLayout->setSpacing(4);
    layerUpButton = new QToolButton(layerTab);
    layerUpButton->setIcon(style()->standardIcon(QStyle::SP_ArrowUp));
    layerUpButton->setToolTip(QStringLiteral("上移当前图层"));
    layerUpButton->setAccessibleName(QStringLiteral("上移当前图层"));
    layerOrderLayout->addWidget(layerUpButton);
    layerDownButton = new QToolButton(layerTab);
    layerDownButton->setIcon(style()->standardIcon(QStyle::SP_ArrowDown));
    layerDownButton->setToolTip(QStringLiteral("下移当前图层"));
    layerDownButton->setAccessibleName(QStringLiteral("下移当前图层"));
    layerOrderLayout->addWidget(layerDownButton);
    layerOrderLayout->addStretch();
    layerLayout->addLayout(layerOrderLayout, 5, 3);
    layerLayout->setColumnStretch(1, 1);
    layerLayout->setColumnStretch(3, 1);
    editTabs->addTab(layerTab, QStringLiteral("图层"));

    auto *resizeTab = new QWidget(editTabs);
    auto *resizeLayout = new QGridLayout(resizeTab);
    resizeLayout->setContentsMargins(8, 8, 8, 8);
    resizeLayout->setHorizontalSpacing(10);
    resizeLayout->setVerticalSpacing(6);
    resizeLayout->addWidget(new QLabel(QStringLiteral("目标宽"), resizeTab), 0, 0);
    resizeWidthSpin = new QSpinBox(resizeTab);
    configurePixelSpin(resizeWidthSpin);
    resizeLayout->addWidget(resizeWidthSpin, 0, 1);
    resizeLayout->addWidget(new QLabel(QStringLiteral("目标高"), resizeTab), 1, 0);
    resizeHeightSpin = new QSpinBox(resizeTab);
    configurePixelSpin(resizeHeightSpin);
    resizeLayout->addWidget(resizeHeightSpin, 1, 1);
    resizeApplyButton = new QPushButton(QStringLiteral("应用缩放"), resizeTab);
    resizeApplyButton->setToolTip(QStringLiteral("使用高质量重采样生成指定像素尺寸的新版本"));
    resizeLayout->addWidget(resizeApplyButton, 2, 0, 1, 2);
    resizeLayout->setColumnStretch(1, 1);
    editTabs->addTab(resizeTab, QStringLiteral("缩放"));
    previewLayout->addWidget(editTabs);

    QWidget *librarySplitterWidget = libraryPane;
    if (useCompactVerticalLayout) {
        auto *libraryScroll = new QScrollArea(splitter);
        libraryScroll->setWidgetResizable(true);
        libraryScroll->setFrameShape(QFrame::NoFrame);
        libraryScroll->setWidget(libraryPane);
        librarySplitterWidget = libraryScroll;
    } else {
        libraryPane->setParent(splitter);
    }
    splitter->addWidget(librarySplitterWidget);
    splitter->addWidget(previewPane);
    splitter->setStretchFactor(0, 0);
    splitter->setStretchFactor(1, 1);
    splitter->setSizes({320, 800});
    rootLayout->addWidget(splitter, 1);

    statusLabel = new QLabel(this);
    statusLabel->setWordWrap(true);
    rootLayout->addWidget(statusLabel);

    connect(projectCombo, qOverload<int>(&QComboBox::currentIndexChanged), this,
            [this](int index) { selectProject(index); });
    connect(newProjectButton, &QToolButton::clicked, this, [this]() { createProject(); });
    connect(refreshButton, &QToolButton::clicked, this, [this]() { refreshProjects(); });
    connect(importButton, &QPushButton::clicked, this, [this]() { importImage(); });
    connect(assetList, &QListWidget::itemSelectionChanged, this, [this]() { selectAsset(); });
    connect(revisionList, &QListWidget::itemSelectionChanged, this, [this]() { selectRevision(); });
    connect(undoButton, &QToolButton::clicked, this, [this]() { navigateHistory(QStringLiteral("undo")); });
    connect(redoButton, &QToolButton::clicked, this, [this]() { navigateHistory(QStringLiteral("redo")); });
    connect(rotateLeftButton, &QPushButton::clicked, this,
            [this]() { createRevision(QStringLiteral("rotate_left")); });
    connect(rotateRightButton, &QPushButton::clicked, this,
            [this]() { createRevision(QStringLiteral("rotate_right")); });
    connect(flipButton, &QPushButton::clicked, this,
            [this]() { createRevision(QStringLiteral("flip_horizontal")); });
    connect(grayscaleButton, &QPushButton::clicked, this,
            [this]() { createRevision(QStringLiteral("grayscale")); });
    connect(aiEditButton, &QPushButton::clicked, this, [this]() { startAiImageEdit(); });
    connect(aiInstructionEdit, &QPlainTextEdit::textChanged, this, [this]() { updateActionState(); });
    connect(colorApplyButton, &QPushButton::clicked, this, [this]() {
        createRevision(
            QStringLiteral("adjust_color"),
            {{QStringLiteral("brightness"), brightnessSpin->value()},
             {QStringLiteral("contrast"), contrastSpin->value()},
             {QStringLiteral("saturation"), saturationSpin->value()}});
    });
    connect(cropApplyButton, &QPushButton::clicked, this, [this]() {
        createRevision(
            QStringLiteral("crop"),
            {{QStringLiteral("x"), cropXSpin->value()},
             {QStringLiteral("y"), cropYSpin->value()},
             {QStringLiteral("width"), cropWidthSpin->value()},
             {QStringLiteral("height"), cropHeightSpin->value()}});
    });
    connect(maskApplyButton, &QPushButton::clicked, this, [this]() {
        createRevision(
            QStringLiteral("apply_rect_mask"),
            {{QStringLiteral("mask_x"), maskXSpin->value()},
             {QStringLiteral("mask_y"), maskYSpin->value()},
             {QStringLiteral("mask_width"), maskWidthSpin->value()},
             {QStringLiteral("mask_height"), maskHeightSpin->value()}});
    });
    connect(layerApplyButton, &QPushButton::clicked, this, [this]() {
        const QString sourceAssetId = layerSourceCombo->currentData().toString();
        if (sourceAssetId.isEmpty()) {
            setStatus(QStringLiteral("请先导入另一张图片作为图层素材。"), true);
            return;
        }
        createRevision(
            QStringLiteral("composite_raster_layer"),
            {{QStringLiteral("overlay_asset_id"), sourceAssetId},
             {QStringLiteral("layer_x"), layerXSpin->value()},
             {QStringLiteral("layer_y"), layerYSpin->value()},
             {QStringLiteral("layer_opacity"), layerOpacitySpin->value()}});
    });
    connect(layerList, &QListWidget::itemChanged, this, [this](QListWidgetItem *) { applyLayerStack(); });
    connect(layerList, &QListWidget::itemSelectionChanged, this, [this]() { updateActionState(); });
    connect(layerUpButton, &QToolButton::clicked, this, [this]() {
        const int row = layerList->currentRow();
        if (row > 0) {
            QListWidgetItem *item = layerList->takeItem(row);
            layerList->insertItem(row - 1, item);
            layerList->setCurrentRow(row - 1);
            applyLayerStack();
        }
    });
    connect(layerDownButton, &QToolButton::clicked, this, [this]() {
        const int row = layerList->currentRow();
        if (row >= 0 && row + 1 < layerList->count()) {
            QListWidgetItem *item = layerList->takeItem(row);
            layerList->insertItem(row + 1, item);
            layerList->setCurrentRow(row + 1);
            applyLayerStack();
        }
    });
    connect(resizeApplyButton, &QPushButton::clicked, this, [this]() {
        createRevision(
            QStringLiteral("resize"),
            {{QStringLiteral("width"), resizeWidthSpin->value()},
             {QStringLiteral("height"), resizeHeightSpin->value()}});
    });
    for (QSpinBox *spin : {brightnessSpin, contrastSpin, saturationSpin}) {
        connect(spin, qOverload<int>(&QSpinBox::valueChanged), this, [this](int) { updateActionState(); });
    }
    for (QSpinBox *spin : {cropXSpin, cropYSpin, cropWidthSpin, cropHeightSpin}) {
        connect(spin, qOverload<int>(&QSpinBox::valueChanged), this, [this](int) {
            updateCropEditorRanges();
            syncCanvasSelection();
            updateActionState();
        });
    }
    for (QSpinBox *spin : {maskXSpin, maskYSpin, maskWidthSpin, maskHeightSpin}) {
        connect(spin, qOverload<int>(&QSpinBox::valueChanged), this, [this](int) {
            updateMaskEditorRanges();
            syncCanvasSelection();
            updateActionState();
        });
    }
    for (QSpinBox *spin : {layerXSpin, layerYSpin, layerOpacitySpin}) {
        connect(spin, qOverload<int>(&QSpinBox::valueChanged), this, [this](int) {
            updateLayerEditorRanges();
            updateActionState();
        });
    }
    connect(layerSourceCombo, qOverload<int>(&QComboBox::currentIndexChanged), this,
            [this](int) { updateActionState(); });
    for (QSpinBox *spin : {resizeWidthSpin, resizeHeightSpin}) {
        connect(spin, qOverload<int>(&QSpinBox::valueChanged), this, [this](int) {
            updateResizeEditorRanges();
            updateActionState();
        });
    }
    connect(exportButton, &QPushButton::clicked, this, [this]() { exportSelectedRevision(); });
    connect(editTabs, &QTabWidget::currentChanged, this,
            [this](int index) { updateCanvasSelectionMode(index); });
    connect(previewCanvas, &MediaImageCanvas::selectionChanged, this,
            [this](const QRect &selection) { applyCanvasSelection(selection); });
    connect(closeButton, &QPushButton::clicked, this, &QDialog::close);

    clearPreview(QStringLiteral("选择一个图片版本以预览"));
    configureGeometryEditors(0, 0);
    updateCanvasSelectionMode(editTabs->currentIndex());
    setStatus(QStringLiteral("正在读取图片项目..."));
    updateActionState();
}

void MediaWorkspaceDialog::connectBackend()
{
    connect(backendClient, &BackendClient::mediaProjectsReceived, this,
            [this](const MediaProjectListResult &result) {
                projects = result.projects;
                populateProjects();
                if (projects.isEmpty()) {
                    setStatus(QStringLiteral("新建一个图片项目后即可导入素材。"));
                }
            });
    connect(backendClient, &BackendClient::mediaProjectCreated, this,
            [this](const MediaProjectInfo &project) {
                activeProjectId = project.projectId;
                activeAssetId.clear();
                selectedRevisionId.clear();
                preferredRevisionId.clear();
                setStatus(QStringLiteral("项目已创建，可以导入图片。"));
                refreshProjects();
                backendClient->requestMediaProject(activeProjectId);
            });
    connect(backendClient, &BackendClient::mediaProjectReceived, this,
            [this](const MediaProjectDetailResult &result) {
                if (result.project.projectId != currentProjectId()) {
                    return;
                }
                projectDetail = result;
                populateAssets();
                updateActionState();
            });
    connect(backendClient, &BackendClient::mediaImageImported, this,
            [this](const MediaImageAssetInfo &asset) {
                const bool belongsToCurrentProject = imageImportPending
                                                     && pendingImportProjectId == currentProjectId();
                imageImportPending = false;
                pendingImportProjectId.clear();
                if (!belongsToCurrentProject) {
                    setStatus(QStringLiteral("图片已导入到原项目；切回该项目即可查看。"));
                    updateActionState();
                    return;
                }
                activeAssetId = asset.assetId;
                preferredRevisionId = asset.currentRevisionId;
                setStatus(QStringLiteral("图片已导入，正在建立版本预览..."));
                backendClient->requestMediaProject(currentProjectId());
                backendClient->requestMediaAssetRevisions(currentProjectId(), asset.assetId);
            });
    connect(backendClient, &BackendClient::mediaAssetRevisionsReceived, this,
            [this](const MediaAssetRevisionListResult &result) {
                if (result.asset.assetId != activeAssetId) {
                    return;
                }
                const bool refreshedAfterConflict = revisionConflictRefreshPending;
                revisionConflictRefreshPending = false;
                currentAsset = result.asset;
                revisions = result.revisions;
                if (revisionRequestPending
                    && !pendingRevisionId.isEmpty()
                    && currentAsset.currentRevisionId == pendingRevisionId) {
                    revisionRequestPending = false;
                    pendingRevisionId.clear();
                }
                if (refreshedAfterConflict) {
                    preferredRevisionId = currentAsset.currentRevisionId;
                }
                populateRevisions();
                if (refreshedAfterConflict) {
                    setStatus(QStringLiteral("图片版本已更新，已同步到当前版本，请重试。"), true);
                }
                updateActionState();
            });
    connect(backendClient, &BackendClient::mediaImageLayerStackReceived, this,
            [this](const MediaImageLayerStackResult &result) {
                if (result.assetId != activeAssetId || result.revisionId != selectedRevisionId) {
                    return;
                }
                layerStackRequestPending = false;
                layerStack = result;
                populateLayerStack();
                updateActionState();
            });
    connect(backendClient, &BackendClient::mediaImageHistoryNavigated, this,
            [this](const QString &action, const MediaAssetRevisionListResult &result) {
                if (!historyNavigationPending || action != pendingHistoryAction || result.asset.assetId != activeAssetId) {
                    return;
                }
                historyNavigationPending = false;
                pendingHistoryAction.clear();
                currentAsset = result.asset;
                revisions = result.revisions;
                preferredRevisionId = currentAsset.currentRevisionId;
                populateRevisions();
                setStatus(action == QStringLiteral("undo")
                              ? QStringLiteral("已撤销到上一个图片版本。")
                              : QStringLiteral("已重做图片编辑。"));
                backendClient->requestMediaProject(currentProjectId());
                updateActionState();
            });
    connect(backendClient, &BackendClient::mediaImageRevisionCreated, this,
            [this](const MediaImageRevisionInfo &revision) {
                if (revision.assetId != activeAssetId) {
                    return;
                }
                pendingRevisionTaskId.clear();
                if (revision.operation == QStringLiteral("adjust_color")) {
                    const QSignalBlocker brightnessBlocker(brightnessSpin);
                    const QSignalBlocker contrastBlocker(contrastSpin);
                    const QSignalBlocker saturationBlocker(saturationSpin);
                    brightnessSpin->setValue(0);
                    contrastSpin->setValue(0);
                    saturationSpin->setValue(0);
                }
                const bool completedAiEdit = pendingAiEdit;
                pendingAiEdit = false;
                if (completedAiEdit) {
                    aiInstructionEdit->clear();
                }
                preferredRevisionId = revision.revisionId;
                pendingRevisionId = revision.revisionId;
                setStatus(completedAiEdit
                              ? QStringLiteral("AI 修图结果已回读验证并生成新的图片版本。")
                              : QStringLiteral("已生成新的图片版本。"));
                backendClient->requestMediaProject(currentProjectId());
                backendClient->requestMediaAssetRevisions(currentProjectId(), activeAssetId);
                updateActionState();
            });
    connect(backendClient, &BackendClient::mediaImageRevisionTaskStarted, this,
            [this](const QString &taskId) {
                if (!revisionRequestPending) {
                    return;
                }
                pendingRevisionTaskId = taskId;
                setStatus(pendingAiEdit
                              ? QStringLiteral("正在提交 AI 修图并等待结果...")
                              : QStringLiteral("正在生成并校验图片版本..."));
                QTimer::singleShot(100, this, [this, taskId]() {
                    if (revisionRequestPending && pendingRevisionTaskId == taskId) {
                        if (pendingAiEdit) {
                            backendClient->requestMediaImageAiEditTaskResult(taskId);
                        } else {
                            backendClient->requestMediaImageRevisionTaskResult(taskId);
                        }
                    }
                });
            });
    connect(backendClient, &BackendClient::mediaImageRevisionStillRunning, this,
            [this](const QString &taskId, const QString &) {
                if (!revisionRequestPending || pendingRevisionTaskId != taskId) {
                    return;
                }
                setStatus(pendingAiEdit
                              ? QStringLiteral("正在等待 AI 修图结果并回读校验...")
                              : QStringLiteral("正在生成并校验图片版本..."));
                QTimer::singleShot(160, this, [this, taskId]() {
                    if (revisionRequestPending && pendingRevisionTaskId == taskId) {
                        if (pendingAiEdit) {
                            backendClient->requestMediaImageAiEditTaskResult(taskId);
                        } else {
                            backendClient->requestMediaImageRevisionTaskResult(taskId);
                        }
                    }
                });
            });
    connect(backendClient, &BackendClient::mediaImageRevisionCancelled, this,
            [this](const QString &message) {
                if (!revisionRequestPending) {
                    return;
                }
                revisionRequestPending = false;
                pendingAiEdit = false;
                pendingRevisionId.clear();
                pendingRevisionTaskId.clear();
                setStatus(message, true);
                updateActionState();
            });
    connect(backendClient, &BackendClient::mediaRevisionPreviewReceived, this,
            [this](const QString &projectId, const QString &revisionId, const QByteArray &content) {
                if (projectId != currentProjectId() || revisionId != selectedRevisionId) {
                    return;
                }
                showPreview(content);
            });
    connect(backendClient, &BackendClient::mediaImageExportTaskStarted, this,
            [this](const QString &taskId) {
                if (pendingSavePath.isEmpty()) {
                    return;
                }
                pendingExportTaskId = taskId;
                setStatus(QStringLiteral("正在导出并校验 PNG..."));
                QTimer::singleShot(120, this, [this, taskId]() {
                    if (pendingExportTaskId == taskId && !pendingSavePath.isEmpty()) {
                        backendClient->requestMediaImageExportTaskResult(taskId);
                    }
                });
            });
    connect(backendClient, &BackendClient::mediaImageExportStillRunning, this,
            [this](const QString &taskId, const QString &) {
                if (pendingExportTaskId != taskId || pendingSavePath.isEmpty()) {
                    return;
                }
                setStatus(QStringLiteral("正在导出并校验 PNG..."));
                QTimer::singleShot(180, this, [this, taskId]() {
                    if (pendingExportTaskId == taskId && !pendingSavePath.isEmpty()) {
                        backendClient->requestMediaImageExportTaskResult(taskId);
                    }
                });
            });
    connect(backendClient, &BackendClient::mediaImageExportCancelled, this,
            [this](const QString &message) {
                if (pendingExportTaskId.isEmpty()) {
                    return;
                }
                pendingSavePath.clear();
                pendingExportId.clear();
                pendingExportTaskId.clear();
                setStatus(message, true);
                updateActionState();
            });
    connect(backendClient, &BackendClient::mediaImageExported, this,
            [this](const MediaImageExportInfo &imageExport) {
                if (imageExport.projectId != currentProjectId() || pendingSavePath.isEmpty()) {
                    return;
                }
                pendingExportTaskId.clear();
                pendingExportId = imageExport.exportId;
                setStatus(QStringLiteral("正在保存导出的 PNG..."));
                backendClient->requestMediaExportDownload(imageExport.projectId, imageExport.exportId);
            });
    connect(backendClient, &BackendClient::mediaExportDownloaded, this,
            [this](const QString &projectId, const QString &exportId, const QByteArray &content) {
                if (projectId != currentProjectId() || exportId != pendingExportId || pendingSavePath.isEmpty()) {
                    return;
                }
                QSaveFile output(pendingSavePath);
                if (!output.open(QIODevice::WriteOnly)
                    || output.write(content) != content.size()
                    || !output.commit()) {
                    setStatus(QStringLiteral("无法保存导出的 PNG：%1").arg(output.errorString()), true);
                } else {
                    setStatus(QStringLiteral("已导出 PNG：%1").arg(QFileInfo(pendingSavePath).fileName()));
                }
                pendingSavePath.clear();
                pendingExportId.clear();
                pendingExportTaskId.clear();
                updateActionState();
            });
    connect(backendClient, &BackendClient::mediaAgentFailed, this,
            [this](const QString &operation, const QString &message) {
        bool refreshAfterConflict = false;
        if (operation == QStringLiteral("import_image")) {
            imageImportPending = false;
            pendingImportProjectId.clear();
        }
        if (operation == QStringLiteral("create_revision")) {
            revisionRequestPending = false;
            pendingAiEdit = false;
            pendingRevisionId.clear();
            pendingRevisionTaskId.clear();
            refreshAfterConflict = message.startsWith(QStringLiteral("HTTP 409"));
        }
        if (operation == QStringLiteral("start_revision_task")
            || operation == QStringLiteral("revision_task_result")
            || operation == QStringLiteral("start_ai_edit_task")
            || operation == QStringLiteral("ai_edit_task_result")) {
            revisionRequestPending = false;
            pendingAiEdit = false;
            pendingRevisionId.clear();
            pendingRevisionTaskId.clear();
        }
        if (operation == QStringLiteral("navigate_history")) {
            historyNavigationPending = false;
            pendingHistoryAction.clear();
            refreshAfterConflict = message.startsWith(QStringLiteral("HTTP 409"));
        }
        if (operation == QStringLiteral("export_revision")
            || operation == QStringLiteral("start_export_task")
            || operation == QStringLiteral("export_task_result")
            || operation == QStringLiteral("download_export")) {
            pendingSavePath.clear();
            pendingExportId.clear();
            pendingExportTaskId.clear();
        }
        if (refreshAfterConflict && !currentProjectId().isEmpty() && !activeAssetId.isEmpty()) {
            revisionConflictRefreshPending = true;
            preferredRevisionId = currentAsset.currentRevisionId;
            setStatus(QStringLiteral("图片版本已更新，正在同步当前版本..."), true);
            backendClient->requestMediaProject(currentProjectId());
            backendClient->requestMediaAssetRevisions(currentProjectId(), activeAssetId);
        } else {
            setStatus(message, true);
        }
        updateActionState();
    });
}

void MediaWorkspaceDialog::refreshProjects()
{
    if (!backendClient) {
        setStatus(QStringLiteral("图片工作区后端不可用。"), true);
        return;
    }
    setStatus(QStringLiteral("正在读取图片项目..."));
    backendClient->requestMediaProjects();
}

void MediaWorkspaceDialog::createProject()
{
    const QString suggestedTitle = QStringLiteral("图片项目 %1")
                                       .arg(QDateTime::currentDateTime().toString(QStringLiteral("yyyyMMdd")));
    QDialog dialog(this);
    dialog.setObjectName(QStringLiteral("mediaWorkspaceNewProjectDialog"));
    dialog.setAccessibleName(QStringLiteral("mediaWorkspaceNewProjectDialog"));
    dialog.setWindowTitle(QStringLiteral("新建图片项目"));

    auto *layout = new QVBoxLayout(&dialog);
    auto *nameLabel = new QLabel(QStringLiteral("项目名称"), &dialog);
    auto *nameInput = new QLineEdit(suggestedTitle, &dialog);
    nameInput->setObjectName(QStringLiteral("mediaWorkspaceProjectNameInput"));
    nameInput->setAccessibleName(QStringLiteral("mediaWorkspaceProjectNameInput"));
    nameInput->selectAll();
    auto *buttons = new QDialogButtonBox(QDialogButtonBox::Cancel, &dialog);
    auto *confirmButton = buttons->addButton(QStringLiteral("创建"), QDialogButtonBox::AcceptRole);
    confirmButton->setObjectName(QStringLiteral("mediaWorkspaceCreateProjectConfirmButton"));
    confirmButton->setAccessibleName(QStringLiteral("mediaWorkspaceCreateProjectConfirmButton"));
    layout->addWidget(nameLabel);
    layout->addWidget(nameInput);
    layout->addWidget(buttons);
    connect(buttons, &QDialogButtonBox::rejected, &dialog, &QDialog::reject);
    connect(confirmButton, &QPushButton::clicked, &dialog, &QDialog::accept);

    if (dialog.exec() != QDialog::Accepted || nameInput->text().trimmed().isEmpty()) {
        return;
    }
    setStatus(QStringLiteral("正在创建图片项目..."));
    backendClient->createMediaProject(nameInput->text());
}

void MediaWorkspaceDialog::selectProject(int index)
{
    if (index < 0 || index >= projects.size()) {
        activeProjectId.clear();
        activeAssetId.clear();
        selectedRevisionId.clear();
        projectDetail = MediaProjectDetailResult{};
        currentAsset = MediaImageAssetInfo{};
        revisions.clear();
        populateAssets();
        populateRevisions();
        clearPreview(QStringLiteral("选择一个图片项目"));
        updateActionState();
        return;
    }
    const QString projectId = projects.at(index).projectId;
    if (projectId == activeProjectId && !projectDetail.project.projectId.isEmpty()) {
        return;
    }
    activeProjectId = projectId;
    activeAssetId.clear();
    selectedRevisionId.clear();
    preferredRevisionId.clear();
    projectDetail = MediaProjectDetailResult{};
    currentAsset = MediaImageAssetInfo{};
    revisions.clear();
    populateAssets();
    populateRevisions();
    clearPreview(QStringLiteral("正在读取项目素材..."));
    setStatus(QStringLiteral("正在读取项目素材..."));
    updateActionState();
    backendClient->requestMediaProject(activeProjectId);
}

void MediaWorkspaceDialog::importImage()
{
    if (imageImportPending) {
        return;
    }
    if (currentProjectId().isEmpty()) {
        QMessageBox::information(this, QStringLiteral("导入图片"), QStringLiteral("请先新建或选择一个图片项目。"));
        return;
    }
    const QString path = selectImageForImport(this);
    if (path.isEmpty()) {
        return;
    }
    const QFileInfo info(path);
    if (info.size() <= 0 || info.size() > MaxImportBytes) {
        QMessageBox::warning(this,
                             QStringLiteral("导入图片"),
                             QStringLiteral("图片必须大于 0 且不超过 20 MB。"));
        return;
    }
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly)) {
        QMessageBox::warning(this, QStringLiteral("导入图片"), QStringLiteral("无法读取所选图片。"));
        return;
    }
    const QByteArray content = file.readAll();
    if (content.size() != info.size()) {
        QMessageBox::warning(this, QStringLiteral("导入图片"), QStringLiteral("读取图片内容不完整。"));
        return;
    }
    imageImportPending = true;
    pendingImportProjectId = currentProjectId();
    setStatus(QStringLiteral("正在导入图片..."));
    updateActionState();
    backendClient->importMediaImage(pendingImportProjectId, info.fileName(), content);
}

void MediaWorkspaceDialog::selectAsset()
{
    const QListWidgetItem *item = assetList->currentItem();
    const QString assetId = item ? item->data(Qt::UserRole).toString() : QString();
    if (assetId.isEmpty()) {
        return;
    }
    activeAssetId = assetId;
    populateLayerSources();
    selectedRevisionId.clear();
    preferredRevisionId.clear();
    currentAsset = MediaImageAssetInfo{};
    revisions.clear();
    layerStack = MediaImageLayerStackResult{};
    layerStackRequestPending = false;
    populateLayerStack();
    populateRevisions();
    clearPreview(QStringLiteral("正在读取图片版本..."));
    setStatus(QStringLiteral("正在读取图片版本..."));
    updateActionState();
    backendClient->requestMediaAssetRevisions(currentProjectId(), activeAssetId);
}

void MediaWorkspaceDialog::selectRevision()
{
    const QListWidgetItem *item = revisionList->currentItem();
    const QString revisionId = item ? item->data(Qt::UserRole).toString() : QString();
    if (revisionId.isEmpty() || currentProjectId().isEmpty()) {
        return;
    }
    selectedRevisionId = revisionId;
    layerStack = MediaImageLayerStackResult{};
    layerStackRequestPending = true;
    populateLayerStack();
    clearPreview(QStringLiteral("正在加载版本预览..."));
    setStatus(QStringLiteral("正在加载版本预览..."));
    updateActionState();
    backendClient->requestMediaRevisionPreview(currentProjectId(), selectedRevisionId);
    backendClient->requestMediaImageLayerStack(currentProjectId(), activeAssetId, selectedRevisionId);
}

void MediaWorkspaceDialog::createRevision(const QString &operation, const QJsonObject &parameters)
{
    if (currentProjectId().isEmpty() || activeAssetId.isEmpty() || revisionRequestPending || historyNavigationPending) {
        return;
    }
    if (selectedRevisionId != currentAsset.currentRevisionId) {
        setStatus(QStringLiteral("请选择当前版本后再继续编辑。"), true);
        updateActionState();
        return;
    }
    revisionRequestPending = true;
    pendingAiEdit = false;
    pendingRevisionId.clear();
    setStatus(QStringLiteral("正在生成%1版本...").arg(operationDisplayName(operation)));
    updateActionState();
    backendClient->startMediaImageRevisionTask(
        currentProjectId(), activeAssetId, operation, selectedRevisionId, parameters);
}

void MediaWorkspaceDialog::startAiImageEdit()
{
    const QString instruction = aiInstructionEdit ? aiInstructionEdit->toPlainText().trimmed() : QString();
    if (currentProjectId().isEmpty() || activeAssetId.isEmpty() || instruction.isEmpty()
        || revisionRequestPending || historyNavigationPending) {
        return;
    }
    if (selectedRevisionId != currentAsset.currentRevisionId) {
        setStatus(QStringLiteral("请选择当前版本后再继续 AI 修图。"), true);
        updateActionState();
        return;
    }
    revisionRequestPending = true;
    pendingAiEdit = true;
    pendingRevisionId.clear();
    setStatus(QStringLiteral("正在受理 AI 修图任务..."));
    updateActionState();
    backendClient->startMediaImageAiEditTask(
        currentProjectId(), activeAssetId, selectedRevisionId, instruction);
}

void MediaWorkspaceDialog::navigateHistory(const QString &action)
{
    const bool canNavigate = action == QStringLiteral("undo")
                                 ? currentAsset.undoAvailable
                                 : action == QStringLiteral("redo") && currentAsset.redoAvailable;
    if (currentProjectId().isEmpty() || activeAssetId.isEmpty() || revisionRequestPending
        || historyNavigationPending || selectedRevisionId != currentAsset.currentRevisionId || !canNavigate) {
        return;
    }
    historyNavigationPending = true;
    pendingHistoryAction = action;
    setStatus(action == QStringLiteral("undo")
                  ? QStringLiteral("正在撤销图片编辑...")
                  : QStringLiteral("正在重做图片编辑..."));
    updateActionState();
    backendClient->navigateMediaImageHistory(
        currentProjectId(), activeAssetId, action, selectedRevisionId);
}

void MediaWorkspaceDialog::exportSelectedRevision()
{
    if (currentProjectId().isEmpty() || selectedRevisionId.isEmpty()) {
        return;
    }
    QString baseName = currentAsset.name;
    const int suffixIndex = baseName.lastIndexOf(QLatin1Char('.'));
    if (suffixIndex > 0) {
        baseName.truncate(suffixIndex);
    }
    const QString suggestedName = baseName.isEmpty()
                                      ? QStringLiteral("edited-image.png")
                                      : QStringLiteral("%1-edited.png").arg(baseName);
    QString path = selectPngExportPath(this, suggestedName);
    if (path.isEmpty()) {
        return;
    }
    if (!path.endsWith(QStringLiteral(".png"), Qt::CaseInsensitive)) {
        path.append(QStringLiteral(".png"));
    }
    pendingSavePath = path;
    pendingExportId.clear();
    pendingExportTaskId.clear();
    setStatus(QStringLiteral("正在准备 PNG 导出..."));
    updateActionState();
    backendClient->startMediaImageExportTask(
        currentProjectId(), selectedRevisionId, QFileInfo(pendingSavePath).fileName());
}

void MediaWorkspaceDialog::populateProjects()
{
    QSignalBlocker blocker(projectCombo);
    projectCombo->clear();
    for (const MediaProjectInfo &project : projects) {
        projectCombo->addItem(
            QStringLiteral("%1 (%2)").arg(project.title).arg(project.assetCount), project.projectId);
    }
    if (projects.isEmpty()) {
        activeProjectId.clear();
        return;
    }
    int selectedIndex = 0;
    for (int index = 0; index < projects.size(); ++index) {
        if (projects.at(index).projectId == activeProjectId) {
            selectedIndex = index;
            break;
        }
    }
    activeProjectId = projects.at(selectedIndex).projectId;
    projectCombo->setCurrentIndex(selectedIndex);
    if (projectDetail.project.projectId != activeProjectId) {
        backendClient->requestMediaProject(activeProjectId);
    }
}

void MediaWorkspaceDialog::populateAssets()
{
    {
        QSignalBlocker blocker(assetList);
        assetList->clear();
        for (const MediaImageAssetInfo &asset : projectDetail.assets) {
            auto *item = new QListWidgetItem(
                QStringLiteral("%1\n%2 | %3 个版本")
                    .arg(asset.name, imageSizeText(asset.width, asset.height))
                    .arg(asset.revisionCount),
                assetList);
            item->setData(Qt::UserRole, asset.assetId);
        }
        if (projectDetail.assets.isEmpty()) {
            activeAssetId.clear();
            currentAsset = MediaImageAssetInfo{};
            revisions.clear();
            selectedRevisionId.clear();
        } else {
            int selectedIndex = 0;
            for (int index = 0; index < projectDetail.assets.size(); ++index) {
                if (projectDetail.assets.at(index).assetId == activeAssetId) {
                    selectedIndex = index;
                    break;
                }
            }
            activeAssetId = projectDetail.assets.at(selectedIndex).assetId;
            assetList->setCurrentRow(selectedIndex);
        }
    }
    if (projectDetail.assets.isEmpty()) {
        populateLayerSources();
        populateRevisions();
        clearPreview(QStringLiteral("导入图片后将在这里显示预览"));
        return;
    }
    selectAsset();
}

void MediaWorkspaceDialog::populateLayerSources()
{
    if (!layerSourceCombo) {
        return;
    }
    const QString selectedSourceId = layerSourceCombo->currentData().toString();
    QSignalBlocker blocker(layerSourceCombo);
    layerSourceCombo->clear();
    for (const MediaImageAssetInfo &asset : projectDetail.assets) {
        if (asset.assetId == activeAssetId) {
            continue;
        }
        layerSourceCombo->addItem(
            QStringLiteral("%1 (%2)").arg(asset.name, imageSizeText(asset.width, asset.height)),
            asset.assetId);
    }
    const int previousIndex = layerSourceCombo->findData(selectedSourceId);
    if (previousIndex >= 0) {
        layerSourceCombo->setCurrentIndex(previousIndex);
    }
}

void MediaWorkspaceDialog::populateLayerStack()
{
    if (!layerList) {
        return;
    }
    const QSignalBlocker blocker(layerList);
    layerList->clear();
    for (const MediaImageLayerInfo &layer : layerStack.layers) {
        auto *item = new QListWidgetItem(
            QStringLiteral("%1 | x=%2, y=%3, %4%")
                .arg(layer.sourceName)
                .arg(layer.x)
                .arg(layer.y)
                .arg(layer.opacity),
            layerList);
        item->setData(Qt::UserRole, layer.layerId);
        item->setFlags(item->flags() | Qt::ItemIsUserCheckable);
        item->setCheckState(layer.visible ? Qt::Checked : Qt::Unchecked);
    }
    if (!layerStack.layers.isEmpty()) {
        layerList->setCurrentRow(layerList->count() - 1);
    }
}

void MediaWorkspaceDialog::populateRevisions()
{
    {
        QSignalBlocker blocker(revisionList);
        revisionList->clear();
        for (const MediaImageRevisionInfo &revision : revisions) {
            auto *item = new QListWidgetItem(revisionLabel(revision), revisionList);
            item->setData(Qt::UserRole, revision.revisionId);
        }
        if (revisions.isEmpty()) {
            selectedRevisionId.clear();
            layerStack = MediaImageLayerStackResult{};
            layerStackRequestPending = false;
            populateLayerStack();
            return;
        }
        const QString desiredRevision = preferredRevisionId.isEmpty()
                                            ? currentAsset.currentRevisionId
                                            : preferredRevisionId;
        int selectedIndex = 0;
        for (int index = 0; index < revisions.size(); ++index) {
            if (revisions.at(index).revisionId == desiredRevision) {
                selectedIndex = index;
                break;
            }
        }
        selectedRevisionId = revisions.at(selectedIndex).revisionId;
        revisionList->setCurrentRow(selectedIndex);
    }
    selectRevision();
}

void MediaWorkspaceDialog::applyLayerStack()
{
    if (!layerStack.editable || layerStackRequestPending || layerStack.layers.isEmpty()
        || layerList->count() != layerStack.layers.size()) {
        return;
    }
    QJsonArray states;
    for (int index = 0; index < layerList->count(); ++index) {
        const QListWidgetItem *item = layerList->item(index);
        const QString layerId = item ? item->data(Qt::UserRole).toString() : QString();
        if (layerId.isEmpty()) {
            return;
        }
        states.append(QJsonObject{
            {QStringLiteral("layer_id"), layerId},
            {QStringLiteral("visible"), item->checkState() == Qt::Checked},
        });
    }
    createRevision(QStringLiteral("recompose_raster_layers"), {{QStringLiteral("layer_stack"), states}});
}

void MediaWorkspaceDialog::showPreview(const QByteArray &content)
{
    QPixmap source;
    if (!source.loadFromData(content)) {
        clearPreview(QStringLiteral("无法解码图片预览"));
        setStatus(QStringLiteral("图片预览不是有效的 PNG。"), true);
        return;
    }
    previewCanvas->setImage(source);
    const auto iterator = std::find_if(revisions.cbegin(), revisions.cend(), [this](const MediaImageRevisionInfo &revision) {
        return revision.revisionId == selectedRevisionId;
    });
    if (iterator != revisions.cend()) {
        configureGeometryEditors(iterator->width, iterator->height);
        syncCanvasSelection();
        const QString parameterSummary = revisionParameterSummary(*iterator);
        previewMetaLabel->setText(
            QStringLiteral("%1 | %2 | %3%4")
                .arg(operationDisplayName(iterator->operation),
                     imageSizeText(iterator->width, iterator->height),
                     iterator->createdAt,
                     parameterSummary.isEmpty() ? QString() : QStringLiteral(" | %1").arg(parameterSummary)));
    }
    setStatus(QStringLiteral("已加载版本预览。"));
}

void MediaWorkspaceDialog::clearPreview(const QString &message)
{
    previewCanvas->clearImage(message);
    previewMetaLabel->setText({});
    configureGeometryEditors(0, 0);
}

void MediaWorkspaceDialog::configureGeometryEditors(int width, int height)
{
    selectedRevisionWidth = qMax(0, width);
    selectedRevisionHeight = qMax(0, height);
    if (!cropXSpin || !cropYSpin || !cropWidthSpin || !cropHeightSpin || !maskXSpin || !maskYSpin
        || !maskWidthSpin || !maskHeightSpin || !layerXSpin || !layerYSpin || !layerOpacitySpin
        || !resizeWidthSpin || !resizeHeightSpin) {
        return;
    }

    const QSignalBlocker cropXBlocker(cropXSpin);
    const QSignalBlocker cropYBlocker(cropYSpin);
    const QSignalBlocker cropWidthBlocker(cropWidthSpin);
    const QSignalBlocker cropHeightBlocker(cropHeightSpin);
    const QSignalBlocker maskXBlocker(maskXSpin);
    const QSignalBlocker maskYBlocker(maskYSpin);
    const QSignalBlocker maskWidthBlocker(maskWidthSpin);
    const QSignalBlocker maskHeightBlocker(maskHeightSpin);
    const QSignalBlocker layerXBlocker(layerXSpin);
    const QSignalBlocker layerYBlocker(layerYSpin);
    const QSignalBlocker layerOpacityBlocker(layerOpacitySpin);
    const QSignalBlocker resizeWidthBlocker(resizeWidthSpin);
    const QSignalBlocker resizeHeightBlocker(resizeHeightSpin);
    if (selectedRevisionWidth < 1 || selectedRevisionHeight < 1) {
        cropXSpin->setRange(0, 0);
        cropYSpin->setRange(0, 0);
        cropWidthSpin->setRange(1, 1);
        cropHeightSpin->setRange(1, 1);
        maskXSpin->setRange(0, 0);
        maskYSpin->setRange(0, 0);
        maskWidthSpin->setRange(1, 1);
        maskHeightSpin->setRange(1, 1);
        layerXSpin->setRange(0, 0);
        layerYSpin->setRange(0, 0);
        layerOpacitySpin->setRange(1, 100);
        layerOpacitySpin->setValue(100);
        resizeWidthSpin->setRange(1, 1);
        resizeHeightSpin->setRange(1, 1);
        return;
    }

    cropXSpin->setRange(0, selectedRevisionWidth - 1);
    cropYSpin->setRange(0, selectedRevisionHeight - 1);
    cropWidthSpin->setRange(1, qMin(10'000, selectedRevisionWidth));
    cropHeightSpin->setRange(1, qMin(10'000, selectedRevisionHeight));
    maskXSpin->setRange(0, selectedRevisionWidth - 1);
    maskYSpin->setRange(0, selectedRevisionHeight - 1);
    maskWidthSpin->setRange(1, qMin(10'000, selectedRevisionWidth));
    maskHeightSpin->setRange(1, qMin(10'000, selectedRevisionHeight));
    layerXSpin->setRange(0, selectedRevisionWidth - 1);
    layerYSpin->setRange(0, selectedRevisionHeight - 1);
    layerOpacitySpin->setRange(1, 100);
    resizeWidthSpin->setRange(1, 10'000);
    resizeHeightSpin->setRange(1, 10'000);
    cropXSpin->setValue(0);
    cropYSpin->setValue(0);
    cropWidthSpin->setValue(qMin(10'000, selectedRevisionWidth));
    cropHeightSpin->setValue(qMin(10'000, selectedRevisionHeight));
    maskXSpin->setValue(0);
    maskYSpin->setValue(0);
    maskWidthSpin->setValue(qMin(10'000, selectedRevisionWidth));
    maskHeightSpin->setValue(qMin(10'000, selectedRevisionHeight));
    layerXSpin->setValue(0);
    layerYSpin->setValue(0);
    layerOpacitySpin->setValue(100);
    resizeWidthSpin->setValue(qMin(10'000, selectedRevisionWidth));
    resizeHeightSpin->setValue(qMin(10'000, selectedRevisionHeight));
    updateCropEditorRanges();
    updateMaskEditorRanges();
    updateLayerEditorRanges();
    updateResizeEditorRanges();
}

void MediaWorkspaceDialog::updateCropEditorRanges()
{
    if (!cropXSpin || !cropYSpin || !cropWidthSpin || !cropHeightSpin) {
        return;
    }
    if (selectedRevisionWidth < 1 || selectedRevisionHeight < 1) {
        cropXSpin->setRange(0, 0);
        cropYSpin->setRange(0, 0);
        cropWidthSpin->setRange(1, 1);
        cropHeightSpin->setRange(1, 1);
        return;
    }
    cropXSpin->setRange(0, selectedRevisionWidth - 1);
    cropYSpin->setRange(0, selectedRevisionHeight - 1);
    cropWidthSpin->setRange(1, qMax(1, qMin(10'000, selectedRevisionWidth - cropXSpin->value())));
    cropHeightSpin->setRange(1, qMax(1, qMin(10'000, selectedRevisionHeight - cropYSpin->value())));
}

void MediaWorkspaceDialog::updateMaskEditorRanges()
{
    if (!maskXSpin || !maskYSpin || !maskWidthSpin || !maskHeightSpin) {
        return;
    }
    if (selectedRevisionWidth < 1 || selectedRevisionHeight < 1) {
        maskXSpin->setRange(0, 0);
        maskYSpin->setRange(0, 0);
        maskWidthSpin->setRange(1, 1);
        maskHeightSpin->setRange(1, 1);
        return;
    }
    maskXSpin->setRange(0, selectedRevisionWidth - 1);
    maskYSpin->setRange(0, selectedRevisionHeight - 1);
    maskWidthSpin->setRange(1, qMax(1, qMin(10'000, selectedRevisionWidth - maskXSpin->value())));
    maskHeightSpin->setRange(1, qMax(1, qMin(10'000, selectedRevisionHeight - maskYSpin->value())));
}

void MediaWorkspaceDialog::updateLayerEditorRanges()
{
    if (!layerXSpin || !layerYSpin || !layerOpacitySpin) {
        return;
    }
    if (selectedRevisionWidth < 1 || selectedRevisionHeight < 1) {
        layerXSpin->setRange(0, 0);
        layerYSpin->setRange(0, 0);
        layerOpacitySpin->setRange(1, 100);
        return;
    }
    layerXSpin->setRange(0, selectedRevisionWidth - 1);
    layerYSpin->setRange(0, selectedRevisionHeight - 1);
    layerOpacitySpin->setRange(1, 100);
}

void MediaWorkspaceDialog::updateResizeEditorRanges()
{
    if (!resizeWidthSpin || !resizeHeightSpin) {
        return;
    }
    const int height = qMax(1, resizeHeightSpin->value());
    const int widthLimit = qMax(1, qMin(10'000, static_cast<int>(40'000'000LL / height)));
    resizeWidthSpin->setRange(1, widthLimit);
    const int width = qMax(1, resizeWidthSpin->value());
    const int heightLimit = qMax(1, qMin(10'000, static_cast<int>(40'000'000LL / width)));
    resizeHeightSpin->setRange(1, heightLimit);
}

void MediaWorkspaceDialog::updateCanvasSelectionMode(int tabIndex)
{
    if (!previewCanvas) {
        return;
    }
    MediaImageCanvas::SelectionMode mode = MediaImageCanvas::SelectionMode::None;
    if (tabIndex == cropTabIndex) {
        mode = MediaImageCanvas::SelectionMode::Crop;
    } else if (tabIndex == maskTabIndex) {
        mode = MediaImageCanvas::SelectionMode::Mask;
    }
    previewCanvas->setSelectionMode(mode);
    syncCanvasSelection();
}

void MediaWorkspaceDialog::syncCanvasSelection()
{
    if (!previewCanvas || !editTabs) {
        return;
    }
    if (editTabs->currentIndex() == cropTabIndex) {
        previewCanvas->setSelection(
            QRect(cropXSpin->value(), cropYSpin->value(), cropWidthSpin->value(), cropHeightSpin->value()));
    } else if (editTabs->currentIndex() == maskTabIndex) {
        previewCanvas->setSelection(
            QRect(maskXSpin->value(), maskYSpin->value(), maskWidthSpin->value(), maskHeightSpin->value()));
    }
}

void MediaWorkspaceDialog::applyCanvasSelection(const QRect &selection)
{
    if (selection.isEmpty() || !editTabs) {
        return;
    }
    if (editTabs->currentIndex() == cropTabIndex) {
        const QSignalBlocker xBlocker(cropXSpin);
        const QSignalBlocker yBlocker(cropYSpin);
        const QSignalBlocker widthBlocker(cropWidthSpin);
        const QSignalBlocker heightBlocker(cropHeightSpin);
        cropXSpin->setValue(selection.x());
        cropYSpin->setValue(selection.y());
        updateCropEditorRanges();
        cropWidthSpin->setValue(selection.width());
        cropHeightSpin->setValue(selection.height());
    } else if (editTabs->currentIndex() == maskTabIndex) {
        const QSignalBlocker xBlocker(maskXSpin);
        const QSignalBlocker yBlocker(maskYSpin);
        const QSignalBlocker widthBlocker(maskWidthSpin);
        const QSignalBlocker heightBlocker(maskHeightSpin);
        maskXSpin->setValue(selection.x());
        maskYSpin->setValue(selection.y());
        updateMaskEditorRanges();
        maskWidthSpin->setValue(selection.width());
        maskHeightSpin->setValue(selection.height());
    } else {
        return;
    }
    updateActionState();
}

void MediaWorkspaceDialog::updateActionState()
{
    const bool hasProject = !currentProjectId().isEmpty();
    const bool hasAsset = !activeAssetId.isEmpty() && !currentAsset.assetId.isEmpty();
    const bool hasRevision = !selectedRevisionId.isEmpty();
    const bool isCurrentRevision = hasAsset && hasRevision
                                   && selectedRevisionId == currentAsset.currentRevisionId;
    const bool canEdit = isCurrentRevision && !revisionRequestPending && !historyNavigationPending;
    const bool hasColorChange = brightnessSpin->value() != 0
                                || contrastSpin->value() != 0
                                || saturationSpin->value() != 0;
    const bool hasCropChange = selectedRevisionWidth > 0 && selectedRevisionHeight > 0
                               && (cropXSpin->value() != 0 || cropYSpin->value() != 0
                                   || cropWidthSpin->value() != selectedRevisionWidth
                                   || cropHeightSpin->value() != selectedRevisionHeight);
    const bool hasResizeChange = selectedRevisionWidth > 0 && selectedRevisionHeight > 0
                                 && (resizeWidthSpin->value() != selectedRevisionWidth
                                     || resizeHeightSpin->value() != selectedRevisionHeight);
    const bool hasMaskChange = selectedRevisionWidth > 0 && selectedRevisionHeight > 0
                               && (maskXSpin->value() != 0 || maskYSpin->value() != 0
                                   || maskWidthSpin->value() != selectedRevisionWidth
                                   || maskHeightSpin->value() != selectedRevisionHeight);
    const bool hasLayerSource = layerSourceCombo->currentIndex() >= 0
                                && !layerSourceCombo->currentData().toString().isEmpty();
    const bool hasAiInstruction = aiInstructionEdit && !aiInstructionEdit->toPlainText().trimmed().isEmpty();
    const bool canRecomposeLayers = canEdit && layerStack.editable && !layerStackRequestPending
                                    && !layerStack.layers.isEmpty();
    const bool selectionLocked = revisionRequestPending || historyNavigationPending;
    projectCombo->setEnabled(!selectionLocked);
    newProjectButton->setEnabled(!selectionLocked);
    refreshButton->setEnabled(!selectionLocked);
    assetList->setEnabled(!selectionLocked);
    revisionList->setEnabled(!selectionLocked);
    importButton->setEnabled(hasProject && !imageImportPending);
    undoButton->setEnabled(canEdit && currentAsset.undoAvailable);
    redoButton->setEnabled(canEdit && currentAsset.redoAvailable);
    rotateLeftButton->setEnabled(canEdit);
    rotateRightButton->setEnabled(canEdit);
    flipButton->setEnabled(canEdit);
    grayscaleButton->setEnabled(canEdit);
    aiInstructionEdit->setEnabled(canEdit);
    aiEditButton->setEnabled(canEdit && hasAiInstruction);
    colorApplyButton->setEnabled(canEdit && hasColorChange);
    cropApplyButton->setEnabled(canEdit && hasCropChange);
    maskApplyButton->setEnabled(canEdit && hasMaskChange);
    layerApplyButton->setEnabled(canEdit && hasLayerSource);
    layerList->setEnabled(canRecomposeLayers);
    const int layerRow = layerList->currentRow();
    layerUpButton->setEnabled(canRecomposeLayers && layerRow > 0);
    layerDownButton->setEnabled(canRecomposeLayers && layerRow >= 0 && layerRow + 1 < layerList->count());
    resizeApplyButton->setEnabled(canEdit && hasResizeChange);
    exportButton->setEnabled(hasRevision && pendingSavePath.isEmpty() && !revisionRequestPending && !historyNavigationPending);
}

void MediaWorkspaceDialog::setStatus(const QString &message, bool isError)
{
    statusLabel->setText(message);
    statusLabel->setStyleSheet(isError
                                   ? QStringLiteral("color: #b3261e;")
                                   : QStringLiteral("color: #56708f;"));
}

QString MediaWorkspaceDialog::revisionLabel(const MediaImageRevisionInfo &revision) const
{
    const QString currentSuffix = revision.revisionId == currentAsset.currentRevisionId
                                      ? QStringLiteral("  当前")
                                      : QString();
    const QString parameterSummary = revisionParameterSummary(revision);
    const QString detail = QStringLiteral("%1 | %2")
                               .arg(imageSizeText(revision.width, revision.height), revision.createdAt);
    return parameterSummary.isEmpty()
               ? QStringLiteral("%1%2\n%3")
                     .arg(operationDisplayName(revision.operation), currentSuffix, detail)
               : QStringLiteral("%1%2\n%3\n%4")
                     .arg(operationDisplayName(revision.operation), currentSuffix, parameterSummary, detail);
}

QString MediaWorkspaceDialog::currentProjectId() const
{
    return activeProjectId;
}
