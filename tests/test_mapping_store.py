"""The mapping store in its other modes: a Delta table in Unity Catalog, and the UC volume storage."""
from conftest import USER, run_phase1
from onboarding_poc.config import load_config
from onboarding_poc.context import Context
from onboarding_poc.seed import seed
from onboarding_poc.service import OnboardingService


def test_uc_table_mode(tmp_path):
    cfg = load_config(path="/nonexistent", storage={"backend": "local", "local_root": str(tmp_path / "s")},
                      warehouse={"local_root": str(tmp_path / "wh")}, mapping_store={"mode": "uc_table"})
    svc = OnboardingService(cfg)
    keys = seed(cfg, svc.ctx.storage)
    s = svc.intake.create("t", "UK", "TESCO", USER)
    svc.intake.add_from_landing(s, [keys["tesco_daily"]], USER)
    run_phase1(svc, s)
    df = svc.ctx.warehouse.query("SELECT * FROM bronze_dev.pos.onboarding_mapping_store")
    assert len(df) == 2 and df["mapping_json"].notna().any()


class _Entry:
    def __init__(self, path, is_dir):
        self.path, self.is_directory = path, is_dir


class _FakeFiles:
    """Stands in for databricks.sdk WorkspaceClient().files."""
    def __init__(self):
        self.f = {}

    def upload(self, path, contents, overwrite=True):
        self.f[path] = contents.read()

    def download(self, path):
        import io
        return type("R", (), {"contents": io.BytesIO(self.f[path])})()

    def get_metadata(self, path):
        if path not in self.f:
            raise FileNotFoundError(path)

    def list_directory_contents(self, d):
        d = d.rstrip("/") + "/"
        kids = {k[len(d):].split("/")[0] for k in self.f if k.startswith(d)}
        if not kids:
            raise FileNotFoundError(d)
        return [_Entry(d + k, (d + k) not in self.f) for k in sorted(kids)]

    def delete(self, path):
        self.f.pop(path)


def test_volume_storage():
    from onboarding_poc.storage import VolumeStorage
    w = type("W", (), {"files": _FakeFiles()})()
    st = VolumeStorage("/Volumes/nablon_poc/onboarding/files/poc", client=w)
    st.write_json("landing/UK/TESCO/a.json", {"x": 1})
    st.write_text("outputs/s/m.sql", "SELECT 1")
    assert st.read_json("landing/UK/TESCO/a.json") == {"x": 1}
    assert st.list("landing") == ["landing/UK/TESCO/a.json"] and st.exists("outputs/s/m.sql")
    assert st.uri("outputs/s/m.sql") == "/Volumes/nablon_poc/onboarding/files/poc/outputs/s/m.sql"
