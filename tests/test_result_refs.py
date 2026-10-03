"""结果图引用保护测试：删除历史 / LRU 腾空间不得误删仍被引用的结果。

运行：python tests/test_result_refs.py
使用独立临时数据目录，覆盖：
1. 缓存命中产生的两条历史共用一张结果图，删其中一条后另一条仍可打开；
2. 仅一条历史引用的结果，随历史一起删除；
3. LRU 自动腾空间时，被历史引用的结果即使最久未用也保留；
4. 历史引用计数统计。
"""
import io
import os
import shutil
import sys
import tempfile
import time
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from PIL import Image  # noqa: E402
except ModuleNotFoundError:
    # 无 Pillow 环境下用最小桩代替：本测试验证的是引用计数与文件联动，
    # 不依赖真实图像处理，只需 Image.new(...).save(buf, 'PNG') 能产出字节即可。
    pil = types.ModuleType("PIL")
    image_mod = types.ModuleType("PIL.Image")

    class _FakeImage:
        format = None

        def __init__(self, mode, size, color=0):
            self.mode, self.size, self.color = mode, size, color

        def convert(self, mode):
            return self

        def resize(self, size, *a, **k):
            return _FakeImage(self.mode, size, self.color)

        def thumbnail(self, *a, **k):
            return None

        def save(self, fp, fmt=None, **k):
            tag = f"{self.mode}-{self.size}-{self.color}".encode()
            data = b"PNGSTUB" + tag
            if hasattr(fp, "write"):
                fp.write(data)
            else:
                with open(fp, "wb") as f:
                    f.write(data)

    class _UnidentifiedImageError(Exception):
        pass

    image_mod.new = _FakeImage
    image_mod.Image = _FakeImage
    image_mod.Resampling = types.SimpleNamespace(LANCZOS=1)
    image_mod.open = lambda p: _FakeImage("RGB", (64, 48))
    image_mod.UnidentifiedImageError = _UnidentifiedImageError
    image_ops = types.ModuleType("PIL.ImageOps")
    image_ops.exif_transpose = lambda img: img
    # 其余子模块只需可导入（本测试不执行真实算法）
    for sub in ("ImageFilter", "ImageChops", "ImageDraw", "ImageEnhance"):
        m = types.ModuleType(f"PIL.{sub}")
        sys.modules[f"PIL.{sub}"] = m
        setattr(pil, sub, m)
    pil.Image = image_mod
    pil.ImageOps = image_ops
    pil.UnidentifiedImageError = _UnidentifiedImageError
    sys.modules["PIL"] = pil
    sys.modules["PIL.Image"] = image_mod
    sys.modules["PIL.ImageOps"] = image_ops
    from PIL import Image  # noqa: E402,F401

from server import config  # noqa: E402
from server.cache import ResultCache  # noqa: E402
from server.history import HistoryManager  # noqa: E402
from server.image_store import ImageStore  # noqa: E402

_passed = 0


def check(name, cond):
    if not cond:
        raise AssertionError(name)
    global _passed
    _passed += 1
    print(f"  ✔ {name}")


def _solid(color):
    return Image.new("RGB", (64, 48), color)


def run_http_flow():
    """端到端：真实 HTTP 跑两次（缓存命中）-> 删一条 -> 另一条结果仍可打开。

    依赖 Flask/Pillow；环境缺失时自动跳过。
    """
    try:
        import flask  # noqa: F401
        from PIL import Image as _RealImage  # noqa: F401
    except ModuleNotFoundError:
        print("\n== HTTP 端到端（跳过：缺少 Flask/Pillow）==")
        return

    print("\n== HTTP 端到端（重复运行 + 删除共享历史）==")
    import importlib
    # api 在导入时按当前 config 路径创建 JsonStore，确保它拿到临时目录
    from server import api as api_mod
    importlib.reload(api_mod)
    client = api_mod.init_app(flask.Flask(__name__)).test_client()

    buf = io.BytesIO()
    Image.new("RGB", (80, 60), (30, 60, 90)).save(buf, "PNG")
    up = client.post("/api/images", data={"files": (io.BytesIO(buf.getvalue()), "h.png")},
                     content_type="multipart/form-data")
    image_id = up.get_json()["saved"][0]["id"]

    nodes = [{"id": "n1", "type": "brightness", "params": {"amount": 10}, "inputs": []}]
    r1 = client.post("/api/run", json={"image_id": image_id, "nodes": nodes}).get_json()
    r2 = client.post("/api/run", json={"image_id": image_id, "nodes": nodes}).get_json()
    check("第二次运行命中缓存", r2["cache_hit"] is True)
    check("两条历史共用同一结果图", r1["result_id"] == r2["result_id"])

    hist = client.get("/api/history").get_json()["history"]
    shared = [h for h in hist if h["result_id"] == r1["result_id"]]
    check("列表标注共享关系", all(h["result_shared"] and h["result_ref_count"] == 2
                                  for h in shared))

    f1 = client.get(f"/api/results/{r1['result_id']}/file")
    check("结果图可打开", f1.status_code == 200 and len(f1.data) > 0)

    del_resp = client.delete(f"/api/history/{r2['history_id']}").get_json()
    check("删除接口返回结果图已保留", del_resp["result_retained"] is True
          and del_resp["result_deleted"] is False)

    f2 = client.get(f"/api/results/{r1['result_id']}/file")
    check("删除一条后另一条历史的结果图仍可打开", f2.status_code == 200
          and f2.data == f1.data)

    # 删掉最后一条引用，结果随之删除
    client.delete(f"/api/history/{r1['history_id']}")
    f3 = client.get(f"/api/results/{r1['result_id']}/file")
    check("最后一条引用删除后结果图 404", f3.status_code == 404)


def main():
    tmp = tempfile.mkdtemp(prefix="result-refs-test-")
    try:
        # 把所有运行时路径切到临时目录
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
        buf = io.BytesIO()
        _solid((10, 20, 30)).save(buf, "PNG")
        rec = image_store.save_upload(buf.getvalue(), "t.png")

        history = HistoryManager()
        cache = ResultCache(
            pinned_checker=lambda rid, exclude=None:
            history.result_ref_count(rid, exclude) > 0)

        print("== 引用计数 ==")
        r1 = cache.put("key-1", _solid((1, 2, 3)))
        h1 = history.add({"image_id": rec["id"], "result_id": r1, "status": "ok"})
        h2 = history.add({"image_id": rec["id"], "result_id": r1, "status": "ok",
                          "cache_hit": True})
        h3 = history.add({"image_id": rec["id"], "result_id": None, "status": "error"})
        check("共享结果引用计数为 2", history.result_ref_count(r1) == 2)
        check("排除正在删除的一条后计数为 1",
              history.result_ref_count(r1, exclude_history_id=h1["id"]) == 1)

        print("\n== 删除共享结果的一条历史 ==")
        p1 = cache.result_path(r1)
        check("删除前结果图存在", p1 and os.path.exists(p1))
        # 与 API DELETE /history/<id> 相同的动作：历史尚未删除，
        # 通过 exclude_history_id 排除正在删除的这一条
        deleted = cache.delete_result(r1, exclude_history_id=h1["id"])
        check("仍被另一条历史引用，联动删除被跳过", deleted is False)
        check("结果图文件仍在", os.path.exists(p1))
        history.delete(h1["id"])
        check("另一条历史仍指向可打开的结果", cache.result_path(r1) is not None)
        check("缓存条目仍在（后续相同组合仍可命中）", cache.get_entry(r1) is not None)

        print("\n== 删除最后一条引用历史 ==")
        deleted = cache.delete_result(r1, exclude_history_id=h2["id"])
        check("无其他引用后联动删除成功", deleted is True)
        history.delete(h2["id"])
        check("结果图文件已删除", not os.path.exists(p1))
        check("缓存条目已删除", cache.get_entry(r1) is None)

        print("\n== 单条历史的结果随历史删除 ==")
        r2 = cache.put("key-2", _solid((4, 5, 6)))
        h4 = history.add({"image_id": rec["id"], "result_id": r2, "status": "ok"})
        p2 = cache.result_path(r2)
        check("唯一引用排除后即无引用，结果被删除",
              cache.delete_result(r2, exclude_history_id=h4["id"]) is True)
        history.delete(h4["id"])
        check("结果图文件已删除", not os.path.exists(p2))

        print("\n== LRU 腾空间保护被历史引用的结果 ==")
        # 先放 3 条（此时上限足够大，put 不触发淘汰），再把上限降到 2 显式腾空间
        config.CACHE_MAX_ENTRIES = 10
        config.CACHE_MAX_BYTES = 256 * 1024 * 1024
        r_old_free = cache.put("free-a", _solid((7, 8, 9)))
        r_pinned = cache.put("pinned", _solid((10, 11, 12)))
        h5 = history.add({"image_id": rec["id"], "result_id": r_pinned,
                          "status": "ok"})
        r_new_free = cache.put("free-b", _solid((13, 14, 15)))
        now = time.time()

        def _age(doc):
            doc = dict(doc)
            for k, e in doc.items():
                e = dict(e)
                if e.get("result_id") == r_pinned:
                    e["last_access"] = now - 10000
                elif e.get("result_id") == r_old_free:
                    e["last_access"] = now - 5000
                else:
                    e["last_access"] = now
                doc[k] = e
            return doc
        cache.store.update(_age)

        config.CACHE_MAX_ENTRIES = 2
        removed = cache.evict_if_needed()
        check("淘汰了 1 条未被引用的结果", removed == 1)
        pin_path = cache.result_path(r_pinned)
        check("被历史引用的结果即使最旧也保留", pin_path is not None
              and os.path.exists(pin_path))
        check("较旧的无引用结果被淘汰", cache.get_entry(r_old_free) is None)
        check("较新的无引用结果保留", cache.get_entry(r_new_free) is not None)
        remaining = cache.list_results()
        check("缓存条目数回落到上限", len(remaining) == 2)

        # 清理历史后，被钉住的结果恢复为可淘汰；再造一条无引用结果使缓存超限，
        # 下一次腾空间时它（仍被人为设为最旧）应被删
        history.delete(h5["id"])
        r_extra = cache.put("free-c", _solid((16, 17, 18)))
        cache.store.update(_age)
        cache.evict_if_needed()
        check("引用全部消失后，原被钉住的结果可被正常淘汰",
              cache.get_entry(r_pinned) is None)
        check("新放入的无引用结果保留", cache.get_entry(r_extra) is not None)

        run_http_flow()

        print(f"\n全部 {_passed} 项断言通过 ✔")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
