"""验收与补充测试：fixtures 现成场景 + 同子集内的合成变体。

运行：python -m unittest test.test_xliff_corpus -v
"""

from __future__ import annotations

import os
import tempfile
import unittest
from xml.etree import ElementTree as ET

from xliff_corpus import extract_file, inspect_document
from xliff_corpus import reasons as R
from xliff_corpus.tokenizer import (
    CLOSE,
    OPEN,
    PLACEHOLDER,
    TEXT,
    pc_containment,
    serialize_tokens,
    tokenize,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "fixtures")
SCOPE = os.path.join(FIX, "scope.json")

NS = 'xmlns="urn:oasis:names:tc:xliff:document:2.0"'


def write_xlf(content: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".xlf")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(content)
    return path


def wrap(body: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<xliff {NS} version="2.0" srcLang="en" trgLang="zh"><file id="f1">\n'
        f"{body}\n</file></xliff>"
    )


def seg_map(doc) -> dict[tuple[str, str], dict]:
    return {
        (r["identity"]["unit_id"], r["identity"]["segment_id"]): r
        for r in doc["manifest"]
    }


def pair_ids(doc) -> set[str]:
    return {p["pair_id"] for p in doc["pairs"]}


def diag_codes(doc) -> set[str]:
    return {d["code"] for d in doc["diagnostics"]}


class FixtureAcceptanceTests(unittest.TestCase):
    """现成 fixtures 的验收覆盖：换序、嵌套 pc、尾部空白、未翻译、禁译、坏引用。"""

    def test_catalog_happy_path(self):
        res = extract_file(os.path.join(FIX, "catalog.xlf"), SCOPE)
        doc = res.to_dict()
        self.assertEqual(res.file_status, "ok")
        self.assertEqual(doc["stats"]["segments_total"], 5)
        self.assertEqual(pair_ids(doc), {"u1/s1", "u2/s1"})
        report = inspect_document(doc)
        self.assertTrue(report["passed"], report["failures"])

    def test_reordering_and_nested_pc_accepted(self):
        doc = extract_file(os.path.join(FIX, "catalog.xlf"), SCOPE).to_dict()
        u1s1 = seg_map(doc)[("u1", "s1")]
        # 译文把 break 移到句首、pc 仍嵌套，仍可训练。
        self.assertTrue(u1s1["trainable"])
        src_types = [t["type"] for t in u1s1["source"]["tokens"]]
        self.assertEqual(src_types[1:3], [OPEN, OPEN])  # 嵌套 pc 的两个 open
        self.assertEqual(
            u1s1["source"]["pc_containment"],
            {"bold": ["inner", "name"], "inner": ["name"]},
        )
        # 换序后 id 集合相同，仅出现顺序不同。
        self.assertEqual(set(u1s1["code_ids"]["source"]),
                         set(u1s1["code_ids"]["target"]))
        self.assertNotEqual(u1s1["code_ids"]["source"],
                            u1s1["code_ids"]["target"])

    def test_trailing_whitespace_preserved(self):
        doc = extract_file(os.path.join(FIX, "catalog.xlf"), SCOPE).to_dict()
        u1s1 = seg_map(doc)[("u1", "s1")]
        self.assertTrue(u1s1["source"]["serialized"].endswith(" "))
        self.assertTrue(u1s1["target"]["serialized"].endswith(" "))
        last = u1s1["source"]["tokens"][-1]
        self.assertEqual(last["type"], TEXT)
        self.assertEqual(last["value"], " ")  # ph 的 tail，未 trim

    def test_untranslated_state_default_initial_and_missing_target(self):
        doc = extract_file(os.path.join(FIX, "catalog.xlf"), SCOPE).to_dict()
        s2 = seg_map(doc)[("u2", "s2")]
        self.assertEqual(s2["state"], "initial")
        self.assertFalse(s2["has_target"])
        self.assertIsNone(s2["target"])
        self.assertIn(R.REASON_TARGET_MISSING, s2["reasons"])
        self.assertIn(R.REASON_STATE_NOT_ACCEPTED, s2["reasons"])
        self.assertFalse(s2["trainable"])
        # 来源不丢：source token 仍在清单里。
        self.assertEqual(s2["source"]["serialized"], "Pending")

    def test_empty_target_distinct_from_missing(self):
        doc = extract_file(os.path.join(FIX, "catalog.xlf"), SCOPE).to_dict()
        s3 = seg_map(doc)[("u2", "s3")]
        self.assertTrue(s3["has_target"])
        self.assertTrue(s3["target_empty"])
        self.assertIsNotNone(s3["target"])
        self.assertEqual(s3["target"]["tokens"], [])
        self.assertEqual(s3["reasons"], [R.REASON_TARGET_EMPTY])
        self.assertNotIn(R.REASON_TARGET_MISSING, s3["reasons"])

    def test_translate_no_excluded_but_listed(self):
        doc = extract_file(os.path.join(FIX, "catalog.xlf"), SCOPE).to_dict()
        u3 = seg_map(doc)[("u3", "s1")]
        self.assertEqual(u3["unit_translate"], "no")
        self.assertFalse(u3["trainable"])
        self.assertEqual(u3["reasons"], [R.REASON_UNIT_TRANSLATE_NO])
        self.assertNotIn("u3/s1", pair_ids(doc))
        # 文本来源仍保留。
        self.assertEqual(u3["source"]["serialized"], "STOP")
        self.assertEqual(u3["target"]["serialized"], "停止")

    def test_bad_reference_isolated_and_located(self):
        path = os.path.join(FIX, "bad-reference.xlf")
        res = extract_file(path, SCOPE)
        doc = res.to_dict()
        self.assertEqual(res.file_status, "ok")  # 片段级错误不拖垮文件
        bad = seg_map(doc)[("u1", "s1")]
        self.assertFalse(bad["trainable"])
        self.assertIn(R.REASON_CODE_MISMATCH, bad["reasons"])
        codes = diag_codes(doc)
        self.assertIn(R.DIAG_MISSING_DATA_REFERENCE, codes)
        self.assertIn(R.DIAG_CODE_REF_MISMATCH, codes)
        # 定位到具体片段与节点路径。
        d = next(d for d in doc["diagnostics"]
                 if d["code"] == R.DIAG_MISSING_DATA_REFERENCE)
        self.assertEqual(d["location"]["unit_id"], "u1")
        self.assertEqual(d["location"]["segment_id"], "s1")
        self.assertIn("ph[@id='name']", d["location"]["path"])
        # 其它片段隔离：u2/s1 照常入表。
        self.assertIn("u2/s1", pair_ids(doc))
        self.assertTrue(inspect_document(doc)["passed"])

    def test_bad_code_moved_out_of_pc_rejected(self):
        path = os.path.join(FIX, "bad-code.xlf")
        doc = extract_file(path, SCOPE).to_dict()
        bad = seg_map(doc)[("u1", "s1")]
        self.assertFalse(bad["trainable"])
        self.assertIn(R.DIAG_PC_CONTAINMENT_MISMATCH, diag_codes(doc))
        # 不是简单“代码缺失”：id 集合其实相同，只是被移出 pc。
        self.assertEqual(set(bad["code_ids"]["source"]),
                         set(bad["code_ids"]["target"]))
        self.assertIn("u2/s1", pair_ids(doc))  # 片段隔离
        # read-back 能重建并发现该不匹配记录与排除原因，报告自身仍自洽。
        report = inspect_document(doc)
        self.assertTrue(report["passed"], report["failures"])


class TokenizerTests(unittest.TestCase):
    def test_text_tail_order_and_no_trim(self):
        el = ET.fromstring(
            f'<source {NS}>a<ph id="p1" dataRef="d1"/>b <pc id="c1" '
            f'dataRefStart="d2" dataRefEnd="d3">x</pc> tail</source>'
        )
        parsed = tokenize(el, "/source")
        values = [(t.type, t.value) for t in parsed.tokens]
        self.assertEqual(
            values,
            [
                (TEXT, "a"),
                (PLACEHOLDER, "ph[p1]"),
                (TEXT, "b "),
                (OPEN, "pc[c1].open"),
                (TEXT, "x"),
                (CLOSE, "pc[c1].close"),
                (TEXT, " tail"),
            ],
        )
        self.assertEqual(parsed.tokens[2].value, "b ")  # 不 trim
        # close 的 pair_index 指回 open。
        close = next(t for t in parsed.tokens if t.type == CLOSE)
        self.assertIs(parsed.tokens[close.pair_index].type, OPEN)

    def test_pc_containment_nested(self):
        el = ET.fromstring(
            f'<source {NS}><pc id="a" dataRefStart="d1" dataRefEnd="d2">'
            f'<ph id="p" dataRef="d3"/><pc id="b" dataRefStart="d1" '
            f'dataRefEnd="d2"><ph id="q" dataRef="d3"/></pc></pc></source>'
        )
        tokens = [t.to_dict() for t in tokenize(el, "/source").tokens]
        self.assertEqual(
            pc_containment(tokens),
            {"a": ["p", "b", "q"], "b": ["q"]},
        )
        # 序列化可逆地保留代码边界。
        s = serialize_tokens(tokens)
        self.assertIn("⟦pc[a].open⟧", s)
        self.assertTrue(s.endswith("⟦pc[a].close⟧"))

    def test_unsupported_inline_tag_event(self):
        el = ET.fromstring(f'<source {NS}>a<bold id="x">b</bold>c</source>')
        parsed = tokenize(el, "/source")
        kinds = [e[0] for e in parsed.events]
        self.assertIn("unsupported", kinds)
        # 文本不丢。
        self.assertEqual(
            [t.value for t in parsed.tokens if t.type == TEXT],
            ["a", "b", "c"],
        )


class CrossSidePolicyTests(unittest.TestCase):
    """同子集合成输入：非法代码关系必须判废，且不得删代码后悄悄接收。"""

    def _extract_body(self, body: str):
        return extract_file(write_xlf(wrap(body)), SCOPE).to_dict()

    def test_extra_code_in_target_rejected(self):
        body = (
            '<unit id="u1"><originalData><data id="d1">X</data>'
            '<data id="d2">Y</data></originalData>'
            '<segment id="s1" state="final">'
            '<source>a<ph id="p" dataRef="d1"/></source>'
            '<target>甲<ph id="p" dataRef="d1"/><ph id="q" dataRef="d2"/></target>'
            '</segment></unit>'
        )
        doc = self._extract_body(body)
        rec = seg_map(doc)[("u1", "s1")]
        self.assertFalse(rec["trainable"])
        self.assertIn(R.DIAG_TARGET_CODE_EXTRA, diag_codes(doc))
        # q 没有被静默删除：token 仍在 target 清单中。
        self.assertIn("q", rec["code_ids"]["target"])
        self.assertEqual(pair_ids(doc), set())

    def test_missing_code_in_target_rejected(self):
        body = (
            '<unit id="u1"><originalData><data id="d1">X</data></originalData>'
            '<segment id="s1" state="final">'
            '<source>a<ph id="p" dataRef="d1"/></source><target>甲</target>'
            '</segment></unit>'
        )
        doc = self._extract_body(body)
        rec = seg_map(doc)[("u1", "s1")]
        self.assertFalse(rec["trainable"])
        self.assertIn(R.DIAG_TARGET_CODE_MISSING, diag_codes(doc))
        self.assertIn("p", rec["code_ids"]["source"])

    def test_code_kind_mismatch_rejected(self):
        body = (
            '<unit id="u1"><originalData><data id="d1">X</data>'
            '<data id="d2">Y</data></originalData>'
            '<segment id="s1" state="final">'
            '<source><ph id="p" dataRef="d1"/></source>'
            '<target><pc id="p" dataRefStart="d1" dataRefEnd="d2"></pc></target>'
            '</segment></unit>'
        )
        doc = self._extract_body(body)
        rec = seg_map(doc)[("u1", "s1")]
        self.assertFalse(rec["trainable"])
        self.assertIn(R.DIAG_CODE_KIND_MISMATCH, diag_codes(doc))

    def test_sibling_reorder_accepted(self):
        body = (
            '<unit id="u1"><originalData><data id="d1">X</data>'
            '<data id="d2">Y</data></originalData>'
            '<segment id="s1" state="final">'
            '<source><ph id="a" dataRef="d1"/><ph id="b" dataRef="d2"/></source>'
            '<target><ph id="b" dataRef="d2"/><ph id="a" dataRef="d1"/></target>'
            '</segment></unit>'
        )
        doc = self._extract_body(body)
        rec = seg_map(doc)[("u1", "s1")]
        self.assertTrue(rec["trainable"], rec["reasons"])
        self.assertEqual(pair_ids(doc), {"u1/s1"})

    def test_duplicate_code_id_within_side_rejected(self):
        body = (
            '<unit id="u1"><originalData><data id="d1">X</data></originalData>'
            '<segment id="s1" state="final">'
            '<source><ph id="p" dataRef="d1"/><ph id="p" dataRef="d1"/></source>'
            '<target><ph id="p" dataRef="d1"/></target>'
            '</segment></unit>'
        )
        doc = self._extract_body(body)
        rec = seg_map(doc)[("u1", "s1")]
        self.assertFalse(rec["trainable"])
        self.assertIn(R.DIAG_DUPLICATE_CODE_ID, diag_codes(doc))

    def test_duplicate_data_id_flagged_and_located(self):
        body = (
            '<unit id="u1"><originalData><data id="d1">X</data>'
            '<data id="d1">Y</data></originalData>'
            '<segment id="s1" state="translated">'
            '<source>a<ph id="p" dataRef="d1"/></source>'
            '<target>甲<ph id="p" dataRef="d1"/></target></segment></unit>'
        )
        doc = self._extract_body(body)
        self.assertIn(R.DIAG_DUPLICATE_DATA_ID, diag_codes(doc))
        rec = seg_map(doc)[("u1", "s1")]
        self.assertFalse(rec["trainable"])
        d = next(d for d in doc["diagnostics"]
                 if d["code"] == R.DIAG_DUPLICATE_DATA_ID
                 and d["location"]["segment_id"] == "s1")
        self.assertEqual(d["location"]["unit_id"], "u1")
        self.assertTrue(doc["code_table"]["u1"]["d1"]["duplicate"])

    def test_unsupported_inline_tag_rejects_segment(self):
        body = (
            '<unit id="u1"><originalData><data id="d1">X</data></originalData>'
            '<segment id="s1" state="final">'
            '<source>a<ec id="e1"/></source><target>甲</target>'
            '</segment></unit>'
        )
        doc = self._extract_body(body)
        rec = seg_map(doc)[("u1", "s1")]
        self.assertFalse(rec["trainable"])
        self.assertIn(R.DIAG_UNSUPPORTED_INLINE_TAG, diag_codes(doc))

    def test_same_display_and_data_id_across_units_not_shared(self):
        body = (
            '<unit id="u1"><originalData><data id="d1">PRICE-A</data></originalData>'
            '<segment id="s1" state="final"><source><ph id="p" dataRef="d1"/></source>'
            '<target><ph id="p" dataRef="d1"/></target></segment></unit>'
            '<unit id="u2"><originalData><data id="d1">PRICE-B</data></originalData>'
            '<segment id="s1" state="final"><source><ph id="p" dataRef="d1"/></source>'
            '<target><ph id="p" dataRef="d1"/></target></segment></unit>'
        )
        doc = self._extract_body(body)
        # 同 id 在两个 unit 独立解析载荷。
        self.assertEqual(doc["code_table"]["u1"]["d1"]["payload"], "PRICE-A")
        self.assertEqual(doc["code_table"]["u2"]["d1"]["payload"], "PRICE-B")
        for pair in doc["pairs"]:
            self.assertEqual(
                pair["code_payloads"]["p"]["refs"]["d1"],
                doc["code_table"][pair["identity"]["unit_id"]]["d1"]["payload"],
            )
        self.assertTrue(inspect_document(doc)["passed"])


class StructuralTests(unittest.TestCase):
    def test_malformed_xml_fails_file(self):
        path = write_xlf(
            f'<xliff {NS}><file id="f1"><unit id="u1"><segment>'
            "<source>oops"
        )
        res = extract_file(path, SCOPE)
        self.assertEqual(res.file_status, "failed")
        self.assertIn(R.DIAG_XML_PARSE_ERROR, diag_codes(res.to_dict()))
        self.assertEqual(res.stats["segments_total"], 0)
        # read-back 同样不得宣称完整成功。
        self.assertFalse(inspect_document(res.to_dict())["passed"])

    def test_too_many_units_fails(self):
        units = "".join(
            f'<unit id="u{i}"><segment id="s1"><source>x{i}</source></segment></unit>'
            for i in range(21)
        )
        res = extract_file(write_xlf(wrap(units)), SCOPE)
        self.assertEqual(res.file_status, "failed")
        self.assertIn(R.DIAG_TOO_MANY_UNITS, diag_codes(res.to_dict()))

    def test_duplicate_unit_id_fails(self):
        body = (
            '<unit id="x"><segment id="s1"><source>a</source></segment></unit>'
            '<unit id="x"><segment id="s1"><source>b</source></segment></unit>'
        )
        res = extract_file(write_xlf(wrap(body)), SCOPE)
        self.assertEqual(res.file_status, "failed")
        self.assertIn(R.DIAG_DUPLICATE_UNIT_ID, diag_codes(res.to_dict()))

    def test_identity_is_unit_plus_segment(self):
        body = (
            '<unit id="u1"><segment id="s1" state="final">'
            '<source>a</source><target>甲</target></segment></unit>'
            '<unit id="u2"><segment id="s1" state="final">'
            '<source>b</source><target>乙</target></segment></unit>'
        )
        doc = extract_file(write_xlf(wrap(body)), SCOPE).to_dict()
        # 两个 segment id 都是 s1，但 unit 不同 → 身份不同，都入表。
        self.assertEqual(pair_ids(doc), {"u1/s1", "u2/s1"})

    def test_reorder_units_and_change_text_equivalent_structure(self):
        body_a = (
            '<unit id="u1"><segment id="s1" state="final">'
            '<source>Alpha</source><target>阿尔法</target></segment></unit>'
            '<unit id="u2"><segment id="s1" state="final">'
            '<source>Beta</source><target>贝塔</target></segment></unit>'
        )
        body_b = body_a.replace(
            '<unit id="u1">', "\x00"
        ).replace('<unit id="u2">', '<unit id="u1">').replace("\x00", '<unit id="u2">')
        doc_a = extract_file(write_xlf(wrap(body_a)), SCOPE).to_dict()
        doc_b = extract_file(write_xlf(wrap(body_b)), SCOPE).to_dict()
        self.assertEqual(pair_ids(doc_a), pair_ids(doc_b))
        # 更换文本后 token 串变化但结构与判定不变。
        body_c = body_a.replace("Alpha", "Alpha 2.0")
        doc_c = extract_file(write_xlf(wrap(body_c)), SCOPE).to_dict()
        self.assertEqual(pair_ids(doc_c), pair_ids(doc_a))
        self.assertEqual(
            seg_map(doc_c)[("u1", "s1")]["source"]["serialized"], "Alpha 2.0"
        )

    def test_add_remove_segments_reflected_in_manifest(self):
        base = (
            '<unit id="u1"><segment id="s1" state="final">'
            '<source>a</source><target>甲</target></segment></unit>'
        )
        doc_one = extract_file(write_xlf(wrap(base)), SCOPE).to_dict()
        two = base + (
            '<unit id="u2"><segment id="s1"><source>b</source></segment></unit>'
        )
        doc_two = extract_file(write_xlf(wrap(two)), SCOPE).to_dict()
        self.assertEqual(doc_one["stats"]["segments_total"], 1)
        self.assertEqual(doc_two["stats"]["segments_total"], 2)
        # 新增的未翻译片段仍在清单但不入表。
        self.assertEqual(doc_two["stats"]["segments_trainable"], 1)
        self.assertIn(("u2", "s1"), seg_map(doc_two))

    def test_state_translated_accepted(self):
        body = (
            '<unit id="u1"><segment id="s1" state="translated">'
            '<source>a</source><target>甲</target></segment></unit>'
        )
        doc = extract_file(write_xlf(wrap(body)), SCOPE).to_dict()
        self.assertTrue(seg_map(doc)[("u1", "s1")]["trainable"])

    def test_missing_source_listed_with_reason(self):
        body = (
            '<unit id="u1"><segment id="s1" state="final">'
            '<target>甲</target></segment></unit>'
        )
        doc = extract_file(write_xlf(wrap(body)), SCOPE).to_dict()
        rec = seg_map(doc)[("u1", "s1")]
        self.assertFalse(rec["trainable"])
        self.assertIn(R.REASON_SOURCE_MISSING, rec["reasons"])
        self.assertIn(R.DIAG_SOURCE_MISSING, diag_codes(doc))
        self.assertIsNone(rec["source"])
        self.assertEqual(pair_ids(doc), set())  # 没有 source 不可能入训练表

    def test_extra_pc_in_target_rejected(self):
        body = (
            '<unit id="u1"><originalData><data id="d1">X</data>'
            '<data id="d2">Y</data></originalData>'
            '<segment id="s1" state="final">'
            '<source>plain</source>'
            '<target><pc id="c" dataRefStart="d1" dataRefEnd="d2">甲</pc></target>'
            '</segment></unit>'
        )
        doc = extract_file(write_xlf(wrap(body)), SCOPE).to_dict()
        rec = seg_map(doc)[("u1", "s1")]
        self.assertFalse(rec["trainable"])
        self.assertIn(R.DIAG_TARGET_CODE_EXTRA, diag_codes(doc))
        # 代码 token 仍在清单，未被删除后悄悄接收。
        self.assertTrue(
            any(t["type"] == OPEN for t in rec["target"]["tokens"])
        )

    def test_entities_decoded_but_not_executed(self):
        body = (
            '<unit id="u1"><originalData>'
            '<data id="d1">&lt;img src="x" onerror="alert(1)"/&gt;</data>'
            '</originalData><segment id="s1" state="final">'
            '<source><ph id="p" dataRef="d1"/></source>'
            '<target><ph id="p" dataRef="d1"/></target></segment></unit>'
        )
        doc = extract_file(write_xlf(wrap(body)), SCOPE).to_dict()
        payload = doc["code_table"]["u1"]["d1"]["payload"]
        # 实体被解码为字符载荷，仅作为字符串保存。
        self.assertEqual(payload, '<img src="x" onerror="alert(1)"/>')
        self.assertTrue(inspect_document(doc)["passed"])


class ReadbackTests(unittest.TestCase):
    def test_readback_rebuilds_both_directions(self):
        doc = extract_file(os.path.join(FIX, "catalog.xlf"), SCOPE).to_dict()
        report = inspect_document(doc)
        self.assertTrue(report["passed"])
        names = {c["name"] for c in report["checks"]}
        self.assertIn("token_stream_readback", names)
        self.assertIn("pc_containment_readback", names)
        self.assertIn("payload_lookup", names)

    def test_corrupted_output_detected(self):
        doc = extract_file(os.path.join(FIX, "catalog.xlf"), SCOPE).to_dict()
        # 人为破坏结果：删掉一个 source token，read-back 必须发现不一致。
        doc["manifest"][0]["source"]["tokens"].pop()
        report = inspect_document(doc)
        self.assertFalse(report["passed"])
        self.assertTrue(
            any(c["name"] == "token_stream_readback" for c in report["failures"])
        )

    def test_pair_payload_lookup_covers_all_refs(self):
        doc = extract_file(os.path.join(FIX, "catalog.xlf"), SCOPE).to_dict()
        pair = next(p for p in doc["pairs"] if p["pair_id"] == "u1/s1")
        self.assertEqual(pair["code_payloads"]["break"]["refs"]["d1"], "<br/>")
        self.assertEqual(pair["code_payloads"]["bold"]["refs"],
                         {"d2": "<b>", "d3": "</b>"})


if __name__ == "__main__":
    unittest.main()
