"""引用保护回归测试：

1. 删除历史：多条历史共用同一张结果图时，删一条不得破坏其它历史的结果；
   最后一条引用删掉后，结果文件与缓存条目才真正清理。
2. LRU 自动腾空间：仍被历史引用的结果不得被淘汰，先淘汰无引用的；
   新写入结果在登记历史前不会被自身的 put->evict 误删。

运行：python tests/test_reference_protection.py
"""
import io
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image  # noqa: E402

from server import config  # noqa: E402
from server.batch import process_image  # noqa: E402
from server.cache import ResultCache  # noqa: E402
from server.history import HistoryManager  # noqa: E402
from server.image_store import ImageStore  # noqa: E402


def _gradient(w=120, h=90):
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        for x in range(w):
            px[x, y] = (x * 255 // w, y * 255 // h, (x + y) * 255 // (w + h))
    return img


def _to_bytes(img):
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


NODES = [
    {"id": "n1", "type": "brightness", "params": {"amount": 25}, "inputs": []},
]


def _setup(tmp):
    """把运行时目录指向临时位置并重建各单例。"""
    config.DATA_DIR = tmp
    config.IMAGES_DIR = os.path.join(tmp, "images")
    config.RESULTS_DIR = os.path.join(tmp, "results")
    config.THUMBS_DIR = os.path.join(tmp, "thumbnails")
    config.CACHE_DIR = os.path.join(tmp, "cache")
    config.META_DIR = os.path.join(tmp, "metadata")
    config.IMAGES_JSON = os.path.join(config.META_DIR, "images.json")
    config.HISTORY_JSON = os.path.join(config.META_DIR, "history.json")
    config.CACHE_JSON = os.path.join(config.META_DIR, "cache.json")
    config.ensure_dirs()

    image_store = ImageStore()
    cache = ResultCache()
    history = HistoryManager()
    # 与 api.py 一致：缓存淘汰前向历史确认引用
    cache.protected_result_ids_provider = history.referenced_result_ids
    rec = image_store.save_upload(_to_bytes(_gradient()), "g.png")
    return image_store, cache, history, rec["id"]


def test_shared_result_delete():
    tmp = tempfile.mkdtemp(prefix="refprot-")
    try:
        image_store, cache, history, image_id = _setup(tmp)

        # 同图同链跑两次：第二次命中缓存，两条历史共用一个 result_id
        r1 = process_image(image_store, cache, history, image_id, NODES, pipeline_name="p")
        r2 = process_image(image_store, cache, history, image_id, NODES, pipeline_name="p")
        assert r1["cache_hit"] is False, r1
        assert r2["cache_hit"] is True, r2
        assert r1["result_id"] == r2["result_id"], "两次运行应共用同一结果"
        rid = r1["result_id"]
        assert cache.result_path(rid) is not None

        # 删掉第一条历史（模拟 DELETE /history/<id> 的逻辑）
        history.delete(r1["history_id"])
        remaining = history.referenced_result_ids()
        removed = cache.delete_result(rid, protected_ids=remaining)
        assert removed is False, "结果仍被另一条历史引用，不得删除"
        assert cache.get_entry(rid) is not None, "缓存条目应保留"
        assert cache.result_path(rid) is not None, "结果文件应保留"
        assert cache.get_entry(rid) is not None
        assert history.get(r2["history_id"])["result_id"] == rid
        assert Image.open(cache.result_path(rid)).size[0] == 120, "另一条历史的结果图应能正常打开"

        # 引用计数：只剩 1 条
        assert history.reference_counts().get(rid) == 1

        # 删掉最后一条引用 -> 真正清理
        history.delete(r2["history_id"])
        removed = cache.delete_result(rid, protected_ids=history.referenced_result_ids())
        assert removed is True, "零引用时应真正删除"
        assert cache.get_entry(rid) is None, "缓存条目应已清除"
        assert cache.result_path(rid) is None, "结果文件应已清除"
        print("✔ 删除历史：共享结果受保护，最后引用删除后才清理")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_eviction_preserves_referenced():
    tmp = tempfile.mkdtemp(prefix="refprot-")
    try:
        config.CACHE_MAX_ENTRIES = 3
        config.CACHE_MAX_BYTES = 256 * 1024 * 1024
        image_store, cache, history, image_id = _setup(tmp)

        # 造 3 个不同键的结果，并各配一条历史引用（走真实 process_image）
        results = []
        for amount in (10, 20, 30):
            nodes = [{"id": "n1", "type": "brightness", "params": {"amount": amount},
                      "inputs": []}]
            r = process_image(image_store, cache, history, image_id, nodes)
            assert r["error"] is None, r["error"]
            results.append(r)

        assert len(cache.list_results()) == 3

        # 把最早创建的结果调为「最久未用」，确保它排在淘汰序列首位
        old = results[0]
        rid_old = old["result_id"]
        cache.store.update(lambda doc: {
            **doc,
            next(k for k, e in doc.items() if e.get("result_id") == rid_old): {
                **next(e for e in doc.values() if e.get("result_id") == rid_old),
                "last_access": 1.0,
            },
        })

        # 上限降到 2：3 个结果全部被历史引用，即便超限也一个都不能淘汰
        config.CACHE_MAX_ENTRIES = 2
        n = cache.evict_if_needed()
        assert n == 0, f"所有结果都被历史引用，不应淘汰，实际淘汰 {n}"
        assert cache.result_path(rid_old) is not None, "被历史引用的最旧结果必须保留"
        assert len(cache.list_results()) == 3

        # 解除最旧结果的历史引用后再淘汰，应只清掉它一个
        history.delete(old["history_id"])
        n = cache.evict_if_needed()
        assert n == 1, f"无引用的最旧结果应被淘汰，实际 {n}"
        assert cache.result_path(rid_old) is None, "无引用结果应已删除"
        for r in results[1:]:
            assert cache.result_path(r["result_id"]) is not None, "仍被引用的结果应保留"
        print("✔ LRU 腾空间：跳过被历史引用的结果，优先清理无引用的")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        config.CACHE_MAX_ENTRIES = 400


def test_put_eviction_does_not_kill_new_result():
    """缓存已满且旧结果全被引用时，新 put 的结果不能被自己触发的淘汰删掉。"""
    tmp = tempfile.mkdtemp(prefix="refprot-")
    try:
        config.CACHE_MAX_ENTRIES = 2
        config.CACHE_MAX_BYTES = 256 * 1024 * 1024
        image_store, cache, history, image_id = _setup(tmp)

        r1 = process_image(image_store, cache, history, image_id,
                           [{"id": "n1", "type": "brightness", "params": {"amount": 1},
                             "inputs": []}])
        r2 = process_image(image_store, cache, history, image_id,
                           [{"id": "n1", "type": "brightness", "params": {"amount": 2},
                             "inputs": []}])
        assert len(cache.list_results()) == 2

        # 第三个键：超条目上限，但前两个都被历史引用
        r3 = process_image(image_store, cache, history, image_id,
                           [{"id": "n1", "type": "brightness", "params": {"amount": 3},
                             "inputs": []}])
        rid3 = r3["result_id"]
        assert cache.get_entry(rid3) is not None, "新结果在历史登记前被自身的淘汰误删"
        assert cache.result_path(rid3) is not None, "新结果文件必须还在"
        assert history.get(r3["history_id"])["result_id"] == rid3
        # 前两个受保护结果同样还在
        assert cache.result_path(r1["result_id"]) is not None
        assert cache.result_path(r2["result_id"]) is not None
        print("✔ 新写入结果在登记历史前不会被自身的淘汰误删")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        config.CACHE_MAX_ENTRIES = 400


def test_history_reference_helpers():
    tmp = tempfile.mkdtemp(prefix="refprot-")
    try:
        _, _, history, _ = _setup(tmp)
        a = history.add({"result_id": "x"})
        b = history.add({"result_id": "x"})
        c = history.add({"result_id": "y"})
        history.add({"result_id": None})  # 失败记录无结果指针

        assert history.reference_counts() == {"x": 2, "y": 1}
        assert history.referenced_result_ids() == {"x", "y"}
        # 按「删除 a 后」统计：x 仍被 b 引用
        assert history.referenced_result_ids(exclude_history_id=a["id"]) == {"x", "y"}
        history.delete(a["id"])
        history.delete(b["id"])
        assert history.referenced_result_ids() == {"y"}
        assert "x" not in history.referenced_result_ids()
        print("✔ 历史引用统计（集合/计数/exclude）正确")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_history_reference_helpers()
    test_shared_result_delete()
    test_eviction_preserves_referenced()
    test_put_eviction_does_not_kill_new_result()
    print("\n全部引用保护测试通过 ✔")
