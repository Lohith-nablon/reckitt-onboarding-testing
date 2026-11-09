"""The generated Databricks SQL loads exactly what the reviewer saw in the Python preview."""
import pandas as pd
import sqlglot

from conftest import USER, run_phase1
from onboarding_poc.core.mapping.apply import apply_mapping
from onboarding_poc.core.mapping.models import MappingSpec
from onboarding_poc.core.mapping.sqlgen import Source, ddl, generate
from onboarding_poc.core.quality.layout import to_logical


def _norm(v):
    if v is None or v is pd.NA or (isinstance(v, float) and v != v):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return str(v)


def test_sql_matches_python_preview(svc, tesco):
    s, f = tesco
    run_phase1(svc, s)
    wh = svc.ctx.warehouse
    for d in svc.mapping.all(s):
        spec = MappingSpec.model_validate(d["spec"])
        table = svc.ctx.contracts(s).tables[spec.target_table]
        frame = svc.ctx.frame(s, d["file_id"], d["sheet"]).astype(object)
        wh.register("__src", frame)
        b = generate(spec, table, target_fqn="t_dev.x.out_" + table.name.split(".")[-1], source=Source(mode="table", table="__src"),
                     submission_id="s1", batch_id="b1", retailer="TESCO", source_file=f.name)
        wh.execute(ddl(table, b.target_fqn))
        wh.execute(f"INSERT INTO {b.target_fqn} ({', '.join('`' + c + '`' for c in b.columns)})\n{b.select_sql}")
        got = to_logical(wh.query(f"SELECT * FROM {b.target_fqn}"), table)
        py = apply_mapping(frame, spec, table)
        if table.kind == "dimension" and table.pk:
            py = py.drop_duplicates(subset=table.pk)
        assert len(got) == len(py)
        for c in py.columns:
            assert sorted(map(str, map(_norm, py[c]))) == sorted(map(str, map(_norm, got[c]))), c


def test_published_sql_is_valid_databricks_sql(svc, tesco):
    s, _ = tesco
    st = run_phase1(svc, s)
    for i in st["items"]:
        sql = svc.ctx.storage.read_text(i["sql_key"])
        stmts = [x for x in sqlglot.parse(sql, read="databricks") if x is not None]
        assert [type(x).__name__ for x in stmts] == ["Delete", "Insert"]
