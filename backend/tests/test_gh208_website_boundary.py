"""GH-208: company website belongs to an independent repository and publisher."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_product_repository_does_not_bundle_the_company_website() -> None:
    assert not (ROOT / 'static_site' / 'company_homepage').exists()
    allowlist = (ROOT / 'scripts' / 'public_allowlist.txt').read_text()
    assert 'static_site/' not in allowlist


def test_production_deploy_has_no_website_copy_or_destination_configuration() -> None:
    script = (ROOT / 'scripts' / 'deploy_server.sh').read_text()
    for retired in ('company_homepage', '_publish_static_dir', 'STATIC_SITE_DIR_NAME', 'NGINX_DST', 'SHARED_DST'):
        assert retired not in script
    assert 'company website publishing is owned by crowntime-website' in script
    assert 'STATIC_SITE_DIR_NAME' not in (ROOT / '.env.example').read_text()


def test_nonproduction_deploy_has_no_website_destination_overrides() -> None:
    script = (ROOT / 'scripts' / 'deploy_nonprod.sh').read_text()
    for retired in ('NONPROD_SHARED_WEBROOT', 'SHARED_DST=', 'NGINX_DST='):
        assert retired not in script
