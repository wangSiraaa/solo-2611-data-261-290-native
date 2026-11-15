"""差分测试：固定种子随机生成子集内文档，全部须通过双路核对（PyLD vs 独立解释器）。"""

import json
import random

from jsonldrel import import_document

CTX = {
    "@base": "https://b.test/",
    "@vocab": "https://v.test/",
    "ex": "https://ex.test/",
    "link": {"@id": "related", "@type": "@id"},
    "labels": {"@id": "name", "@container": "@language"},
}
WORDS = ["a", "b", "c", "茶", "x"]


def _rand_value(rng, depth, in_list):
    r = rng.random()
    if depth > 2 or r < 0.3:
        return rng.choice([1, 2.5, True, "s", "https://u.test/x"])
    if r < 0.45 and not in_list:
        return {"@list": [_rand_value(rng, depth + 1, True) for _ in range(rng.randint(0, 4))]}
    if r < 0.6:
        return {"@value": "lit", "@language": "en"}
    if r < 0.7:
        return {"@id": rng.choice(["n1", "ex:n2", "_:u1"])}
    if r < 0.85:
        node = {"n": rng.choice(WORDS)}
        if rng.random() < 0.5:
            node["@context"] = {"n": "https://local.test/n"}
        return node
    if in_list:
        return rng.choice(WORDS)
    return [_rand_value(rng, depth + 1, False) for _ in range(rng.randint(1, 3))]


def test_differential_fuzz(tmp_path):
    rng = random.Random(20261115)
    for i in range(120):
        body = {}
        for j in range(rng.randint(1, 5)):
            body[f"p{j}"] = _rand_value(rng, 0, False)
        body["labels"] = {"en": [rng.choice(WORDS), rng.choice(WORDS)]}
        if rng.random() < 0.3:
            body["@reverse"] = {"related": {"@id": "n9"}}
        if rng.random() < 0.5:
            body["@id"] = "root"
        doc = {"@context": CTX, **body}
        path = tmp_path / f"fuzz{i}.jsonld"
        path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        result = import_document(path)
        assert result.ok, (
            f"第 {i} 个随机文档未通过：{[d.message for d in result.diagnostics]}\n"
            f"文档：{json.dumps(doc, ensure_ascii=False)}"
        )
