# 第三方来源与说明

## AutoBaoMiGuan

- 上游：[NB-XX/AutoBaoMiGuan](https://github.com/NB-XX/AutoBaoMiGuan)
- 许可证：MIT
- 版权：Copyright (c) 2025 NB-XX

本项目的保密观登录、课程与考试业务接口实现参考并改编自该项目。原作者的 MIT 版权与许可正文保留在本仓库 [LICENSE](LICENSE) 中。

新增的终端菜单、年度活动发现、任务状态控制、系统凭据存储及发行检查/校验下载由本项目维护。

## 运行与构建依赖

程序使用 Rich、requests、keyring、PyCryptodome 和 qrcode 等开源组件；Windows 发行程序由 PyInstaller 打包。各组件及其传递依赖保留各自许可证，不因本项目采用 MIT 而改变。

发行程序内包含其所使用组件随包提供的许可信息。本项目未打包保密观课程、试卷、账号、会话凭据或证书。
