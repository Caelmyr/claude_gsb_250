"""处理历史与流水线版本记录。

每次「运行」都落一条历史：完整保留当时的流水线快照（版本）、参数、结果指针、
耗时与逐节点状态。这样历史页既能回看结果，也能一键把旧版本流水线恢复成当前可编辑版本。
"""
import uuid

from . import config
from .storage import JsonStore, now_iso


class HistoryManager:
    def __init__(self):
        self.store = JsonStore(config.HISTORY_JSON, [])

    def add(self, entry):
        """在最前插入一条历史，超出上限裁掉最旧。"""
        entry.setdefault("id", uuid.uuid4().hex)
        entry.setdefault("created_at", now_iso())

        def _upd(doc):
            doc = list(doc)
            doc.insert(0, entry)
            return doc[:config.HISTORY_MAX_ENTRIES]

        self.store.update(_upd)
        return entry

    def list(self, limit=None):
        doc = self.store.read()
        return doc[:limit] if limit else doc

    def get(self, history_id):
        for e in self.store.read():
            if e.get("id") == history_id:
                return e
        return None

    def delete(self, history_id):
        def _upd(doc):
            return [e for e in doc if e.get("id") != history_id]
        self.store.update(_upd)
        return True

    # ---------------------------------------------------------- 结果引用计数
    def result_ref_counts(self):
        """返回 {result_id: 引用该结果的历史条数}。

        缓存命中时同一张结果图会被多条历史共用，计数 > 1 即「共享结果」。
        删除结果文件 / LRU 淘汰前必须据此判断结果是否还活着。
        """
        counts = {}
        for e in self.store.read():
            rid = e.get("result_id")
            if rid:
                counts[rid] = counts.get(rid, 0) + 1
        return counts

    def result_ref_count(self, result_id, exclude_history_id=None):
        """统计某张结果图还被多少条历史引用；可排除一条正在删除的历史。"""
        if not result_id:
            return 0
        return sum(
            1 for e in self.store.read()
            if e.get("result_id") == result_id and e.get("id") != exclude_history_id
        )

    def restore_snapshot(self, history_id):
        """返回某条历史里的流水线快照（供恢复版本）。"""
        e = self.get(history_id)
        if not e:
            return None
        return e.get("pipeline_snapshot")

    def clear(self):
        self.store.write([])
