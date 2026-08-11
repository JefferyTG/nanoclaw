"""豆包端到端全双工实时语音（TASK-037）客户端模块。

- ``client.py``：WebSocket 会话管理（连接 / session.create / 收发 / 优雅关闭）；
- ``uplink.py``：16k PCM 20ms/包（640B）Base64 上行；
- ``downlink.py``：下行音频直通播放，打断由豆包服务端动态判停；
- ``fc_bridge.py``：Function Calling 桥接骨架（tools 空数组 + call_id 配对执行器预留）。

本模块只依赖 ``websockets`` / ``sounddevice``（复用旧 voice 音频栈），
不接入消息总线 / Gateway——全双工对话发生在豆包服务端内部。
"""
