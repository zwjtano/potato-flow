# 任务与人工审核

录制完成后的任务都在“上传任务”查看，直播录制页不保留已经结束的任务下拉框。

<figure class="local-ui-shot">
  <img src="../../assets/screenshots/local-tasks-v1619.webp" alt="本地上传任务页面，展示多个虚构任务及处理状态">
  <figcaption>上传任务页集中展示处理中、失败、仅录制和已完成状态；人物与任务内容全部为虚构。</figcaption>
</figure>

## 查看实际进度

点击某一行的处理进度，会打开逐步详情：

<figure class="local-ui-shot">
  <img src="../../assets/screenshots/local-task-detail-v1619.webp" alt="虚构任务的逐步处理详情">
  <figcaption>详情弹窗保留每一步的状态、产物与错误证据；示例任务为虚构。</figcaption>
</figure>

每个步骤可以查看开始时间、完成时间、实际产物、错误和运行日志。详情弹窗打开时，任务页面不会自动刷新打断阅读；需要最新数据时点击“刷新详情”。

## 失败与人工审核

<figure class="local-ui-shot">
  <img src="../../assets/screenshots/local-manual-review-v1619.webp" alt="本地人工审核页面中的虚构失败任务">
  <figcaption>失败任务可进入人工审核继续编辑或重试；页面中的人物、标题和任务均为虚构。</figcaption>
</figure>

任一步骤失败后：

- 任务保留失败原因和已生成的 AI 内容。
- 可点击重试，已有 AI 内容会作为默认值。
- 需要人工处理时进入“人工审核”，编辑标题、简介、标签、分区和封面后再投稿。

## 删除任务

所有任务均有删除按钮。删除时根据弹窗选择是否一并删除关联的录播、XML、ASS 和封面文件。
