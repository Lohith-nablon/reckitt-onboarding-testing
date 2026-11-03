"""M1 intake paths (upload, S3 with client credentials, mapping file) and the S3 storage backend."""
import boto3
import pytest
from moto import mock_aws

from conftest import USER
from onboarding_poc.config import S3Settings
from onboarding_poc.modules.m1_intake import IntakeError
from onboarding_poc.storage import S3Storage


def test_rejects_bad_files(svc):
    s = svc.intake.create("x", "UK", "TESCO", USER)
    with pytest.raises(IntakeError):
        svc.intake.add_upload(s, "a.exe", b"xx", USER)
    data = svc.ctx.storage.read_bytes(svc.samples["tesco_daily"])
    svc.intake.add_upload(s, "a.csv", data, USER)
    with pytest.raises(IntakeError, match="same file"):
        svc.intake.add_upload(s, "b.csv", data, USER)


def test_mapping_file_sets_target_schema(svc):
    s = svc.intake.create("x", "UK", "TESCO", USER)
    schema = ("table,column,data_type,mandatory\n"
              "bronze.sales_lite,week_ending,date,true\nbronze.sales_lite,retailer_sku_code,string,true\n"
              "bronze.sales_lite,units_sold,decimal,true\nbronze.sales_lite,net_value,decimal,false\n")
    out = svc.intake.set_target_schema(s, "target.csv", schema.encode(), USER)
    assert out["tables"] == ["bronze.sales_lite"]
    assert list(svc.ctx.contracts(s).tables) == ["bronze.sales_lite"]
    svc.intake.add_upload(s, "t.csv", svc.ctx.storage.read_bytes(svc.samples["tesco_daily"]), USER)
    maps = svc.analyse(s, USER)["mappings"]
    assert {m["target_table"] for m in maps} == {"bronze.sales_lite"}


@mock_aws
def test_s3_storage_and_s3_intake(svc):
    c = boto3.client("s3", region_name="eu-north-1")
    c.create_bucket(Bucket="poc", CreateBucketConfiguration={"LocationConstraint": "eu-north-1"})
    st = S3Storage(S3Settings(bucket="poc", prefix="p", region="eu-north-1"), client=c)
    st.write_json("a/b.json", {"x": 1})
    assert st.read_json("a/b.json") == {"x": 1} and st.exists("a/b.json") and st.list("a") == ["a/b.json"]
    c.create_bucket(Bucket="client", CreateBucketConfiguration={"LocationConstraint": "eu-north-1"})
    c.put_object(Bucket="client", Key="drop/tesco.csv", Body=svc.ctx.storage.read_bytes(svc.samples["tesco_daily"]))
    s = svc.intake.create("x", "UK", "TESCO", USER)
    fs = svc.intake.add_from_s3(s, "s3://client/drop/", USER, access_key="k", secret_key="s", region="eu-north-1")
    assert len(fs) == 1 and fs[0].source == "s3://client/drop/tesco.csv"
