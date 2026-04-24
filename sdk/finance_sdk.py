"""
企业微信会话内容存档 C SDK 的 Python ctypes 封装
官方SDK: libWeWorkFinanceSdk_C.so（仅支持 Linux/Windows）
"""

import ctypes
import json
import logging
import os

logger = logging.getLogger(__name__)


class FinanceSDK:
    """企业微信会话内容存档SDK封装"""

    def __init__(self, lib_path: str):
        """
        加载C SDK动态库
        Args:
            lib_path: libWeWorkFinanceSdk_C.so 的文件路径
        """
        if not os.path.exists(lib_path):
            raise FileNotFoundError(f"SDK库文件不存在: {lib_path}")

        self.dll = ctypes.cdll.LoadLibrary(lib_path)
        self.sdk = None

        # 定义 C 函数签名
        self._setup_functions()

    def _setup_functions(self):
        """设置C函数的参数和返回值类型"""
        # NewSdk() -> void*
        self.dll.NewSdk.restype = ctypes.c_void_p

        # DestroySdk(void* sdk) -> void
        self.dll.DestroySdk.argtypes = [ctypes.c_void_p]

        # Init(void* sdk, char* corpid, char* secret) -> int
        self.dll.Init.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]
        self.dll.Init.restype = ctypes.c_int

        # GetChatData(void* sdk, uint64 seq, uint32 limit,
        #             char* proxy, char* passwd, int timeout, void* chatData) -> int
        self.dll.GetChatData.argtypes = [
            ctypes.c_void_p, ctypes.c_uint64, ctypes.c_uint32,
            ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p
        ]
        self.dll.GetChatData.restype = ctypes.c_int

        # DecryptData(char* encrypt_key, char* encrypt_data, void* msg) -> int
        self.dll.DecryptData.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p]
        self.dll.DecryptData.restype = ctypes.c_int

        # GetMediaData(void* sdk, char* sdkfileid, char* proxy, char* passwd,
        #              int timeout, char* isfinish, void* mediaData) -> int
        self.dll.GetMediaData.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p,
            ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p
        ]
        self.dll.GetMediaData.restype = ctypes.c_int

        # NewSlice() -> void*
        self.dll.NewSlice.restype = ctypes.c_void_p

        # FreeSlice(void* slice) -> void
        self.dll.FreeSlice.argtypes = [ctypes.c_void_p]

        # GetContentFromSlice(void* slice) -> char*
        self.dll.GetContentFromSlice.argtypes = [ctypes.c_void_p]
        self.dll.GetContentFromSlice.restype = ctypes.c_char_p

        # GetSliceLen(void* slice) -> int
        self.dll.GetSliceLen.argtypes = [ctypes.c_void_p]
        self.dll.GetSliceLen.restype = ctypes.c_int

        # NewMediaData() -> void*
        self.dll.NewMediaData.restype = ctypes.c_void_p

        # FreeMediaData(void* mediaData) -> void
        self.dll.FreeMediaData.argtypes = [ctypes.c_void_p]

        # GetOutIndexBuf(void* mediaData) -> char*
        self.dll.GetOutIndexBuf.argtypes = [ctypes.c_void_p]
        self.dll.GetOutIndexBuf.restype = ctypes.c_char_p

        # GetData(void* mediaData) -> void*
        self.dll.GetData.argtypes = [ctypes.c_void_p]
        self.dll.GetData.restype = ctypes.c_void_p

        # GetIndexLen(void* mediaData) -> int
        self.dll.GetIndexLen.argtypes = [ctypes.c_void_p]
        self.dll.GetIndexLen.restype = ctypes.c_int

        # GetDataLen(void* mediaData) -> int
        self.dll.GetDataLen.argtypes = [ctypes.c_void_p]
        self.dll.GetDataLen.restype = ctypes.c_int

        # IsMediaDataFinish(void* mediaData) -> int
        self.dll.IsMediaDataFinish.argtypes = [ctypes.c_void_p]
        self.dll.IsMediaDataFinish.restype = ctypes.c_int

    def init(self, corpid: str, secret: str) -> int:
        """
        初始化SDK
        返回: 0表示成功
        """
        self.sdk = self.dll.NewSdk()
        ret = self.dll.Init(self.sdk, corpid.encode("utf-8"), secret.encode("utf-8"))
        if ret != 0:
            logger.error("SDK初始化失败, 错误码: %d", ret)
        else:
            logger.info("SDK初始化成功")
        return ret

    def get_chat_data(self, seq: int, limit: int, proxy: str = "", passwd: str = "", timeout: int = 10) -> tuple[int, dict]:
        """
        拉取聊天记录
        Args:
            seq: 起始序号，首次传0
            limit: 拉取条数，最大1000
            proxy: 代理地址
            passwd: 代理密码
            timeout: 超时秒数
        返回: (错误码, 数据字典)
        """
        chat_data_slice = self.dll.NewSlice()
        try:
            ret = self.dll.GetChatData(
                self.sdk, ctypes.c_uint64(seq), ctypes.c_uint32(limit),
                proxy.encode("utf-8"), passwd.encode("utf-8"),
                ctypes.c_int(timeout), chat_data_slice
            )
            if ret != 0:
                logger.error("拉取聊天记录失败, seq=%d, 错误码: %d", seq, ret)
                return ret, {}

            content = self.dll.GetContentFromSlice(chat_data_slice)
            data = json.loads(content.decode("utf-8"))
            return 0, data
        finally:
            self.dll.FreeSlice(chat_data_slice)

    def decrypt_data(self, encrypt_key: str, encrypt_data: str) -> tuple[int, str]:
        """
        解密聊天内容
        Args:
            encrypt_key: 经RSA解密后的消息密钥
            encrypt_data: 加密的消息内容
        返回: (错误码, 解密后的JSON字符串)
        """
        msg_slice = self.dll.NewSlice()
        try:
            ret = self.dll.DecryptData(
                encrypt_key.encode("utf-8"),
                encrypt_data.encode("utf-8"),
                msg_slice
            )
            if ret != 0:
                logger.error("解密消息失败, 错误码: %d", ret)
                return ret, ""

            content = self.dll.GetContentFromSlice(msg_slice)
            return 0, content.decode("utf-8")
        finally:
            self.dll.FreeSlice(msg_slice)

    def get_media_data(self, sdk_file_id: str, proxy: str = "", passwd: str = "",
                       timeout: int = 10, save_path: str = "") -> tuple[int, str]:
        """
        下载媒体文件（分块下载）
        Args:
            sdk_file_id: 媒体文件的sdkfileid
            proxy: 代理地址
            passwd: 代理密码
            timeout: 超时秒数
            save_path: 保存路径
        返回: (错误码, 保存路径)
        """
        index_buf = b""

        # 确保保存目录存在
        save_dir = os.path.dirname(save_path)
        if save_dir:
            os.makedirs(save_dir, exist_ok=True)

        with open(save_path, "wb") as f:
            while True:
                media_data = self.dll.NewMediaData()
                try:
                    ret = self.dll.GetMediaData(
                        self.sdk,
                        index_buf,
                        sdk_file_id.encode("utf-8"),
                        proxy.encode("utf-8"),
                        passwd.encode("utf-8"),
                        ctypes.c_int(timeout),
                        media_data
                    )
                    if ret != 0:
                        logger.error("下载媒体文件失败, 错误码: %d", ret)
                        return ret, ""

                    # 写入数据
                    data_len = self.dll.GetDataLen(media_data)
                    data_ptr = self.dll.GetData(media_data)
                    f.write(ctypes.string_at(data_ptr, data_len))

                    # 检查是否下载完成
                    is_finish = self.dll.IsMediaDataFinish(media_data)
                    if is_finish == 1:
                        break

                    # 获取下一块的索引
                    index_buf = self.dll.GetOutIndexBuf(media_data)
                finally:
                    self.dll.FreeMediaData(media_data)

        logger.info("媒体文件下载完成: %s", save_path)
        return 0, save_path

    def destroy(self):
        """销毁SDK实例"""
        if self.sdk:
            self.dll.DestroySdk(self.sdk)
            self.sdk = None
            logger.info("SDK已销毁")
