"""Settings: the boundary where a misconfiguration should stop the service."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from api.config import Settings


class TestDatabaseUrl:
    def test_a_plain_postgres_url_gets_the_async_driver(self) -> None:
        # Operators paste what their provider gives them; failing at connect
        # time with a driver error nobody wrote is a poor welcome.
        settings = Settings(database_url="postgresql://u:p@host/db")  # type: ignore[arg-type]

        assert settings.async_database_url().startswith("postgresql+asyncpg://")

    def test_an_explicit_async_url_is_left_alone(self) -> None:
        settings = Settings(database_url="postgresql+asyncpg://u:p@host/db")  # type: ignore[arg-type]

        assert settings.async_database_url() == "postgresql+asyncpg://u:p@host/db"

    def test_a_psycopg_url_is_rewritten(self) -> None:
        # The pipeline uses psycopg; someone will copy its URL across.
        settings = Settings(database_url="postgresql+psycopg://u:p@host/db")  # type: ignore[arg-type]

        assert settings.async_database_url() == "postgresql+asyncpg://u:p@host/db"

    def test_a_missing_url_is_fatal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("API_DATABASE_URL", raising=False)

        with pytest.raises(ValidationError, match="database_url"):
            Settings()  # type: ignore[call-arg]


class TestUnknownVariables:
    def test_a_misspelled_variable_is_fatal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The failure this guard exists for.

        extra="forbid" cannot catch it: the environment source only reads
        variables it already has a field for, so the typo is ignored, the
        default is used, and the service starts up quietly misconfigured.
        """
        monkeypatch.setenv("API_POOL_SIZ", "40")

        with pytest.raises(ValidationError, match="API_POOL_SIZ"):
            Settings()  # type: ignore[call-arg]

    def test_a_correctly_named_variable_is_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("API_POOL_SIZE", "7")

        assert Settings().pool_size == 7  # type: ignore[call-arg]

    def test_unrelated_variables_are_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Cloud Run injects its own; only the API_ namespace is ours to police.
        monkeypatch.setenv("PORT", "8080")
        monkeypatch.setenv("K_SERVICE", "catalogue-api")

        assert Settings()  # type: ignore[call-arg]


class TestBounds:
    @pytest.mark.parametrize(
        ("field", "value"),
        [("pool_size", 0), ("pool_size", 21), ("statement_timeout_ms", 50), ("max_page_size", 0)],
    )
    def test_out_of_range_values_are_rejected(self, field: str, value: int) -> None:
        with pytest.raises(ValidationError):
            Settings(database_url="postgresql://u:p@h/d", **{field: value})  # type: ignore[arg-type]

    def test_a_default_page_larger_than_the_maximum_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="default_page_size"):
            Settings(  # type: ignore[arg-type]
                database_url="postgresql://u:p@h/d", default_page_size=50, max_page_size=20
            )

    def test_mcp_defaults_must_fit_inside_the_mcp_cap(self) -> None:
        with pytest.raises(ValidationError, match="mcp_default_results"):
            Settings(  # type: ignore[arg-type]
                database_url="postgresql://u:p@h/d", mcp_default_results=40, mcp_max_results=10
            )

    def test_the_mcp_cap_is_far_below_the_http_page_cap(self) -> None:
        # An MCP result lands in a context window, an HTTP page does not.
        settings = Settings()  # type: ignore[call-arg]

        assert settings.mcp_max_results < settings.max_page_size

    def test_an_unknown_log_level_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="log_level"):
            Settings(database_url="postgresql://u:p@h/d", log_level="CHATTY")  # type: ignore[arg-type]

    def test_log_level_is_normalised(self) -> None:
        assert Settings(database_url="postgresql://u:p@h/d", log_level="debug").log_level == "DEBUG"  # type: ignore[arg-type]


class TestDriverParameterTranslation:
    """libpq query parameters that asyncpg does not accept.

    Only reachable against a database that actually uses TLS, so a local
    container never triggers them and every managed provider does. This cost a
    500 on the first deploy.
    """

    def test_sslmode_becomes_ssl(self) -> None:
        settings = Settings(  # type: ignore[arg-type]
            database_url="postgresql://u:p@host/db?sslmode=require"
        )

        assert "ssl=require" in settings.async_database_url()
        assert "sslmode" not in settings.async_database_url()

    def test_tls_is_not_silently_dropped(self) -> None:
        # Dropping sslmode instead of translating it would downgrade to
        # plaintext against a provider that requires TLS — a worse failure than
        # the TypeError, because it succeeds.
        settings = Settings(  # type: ignore[arg-type]
            database_url="postgresql://u:p@host/db?sslmode=require"
        )

        assert "ssl=" in settings.async_database_url()

    def test_channel_binding_is_dropped(self) -> None:
        # asyncpg negotiates it automatically and rejects the argument.
        settings = Settings(  # type: ignore[arg-type]
            database_url="postgresql://u:p@host/db?sslmode=require&channel_binding=require"
        )

        assert "channel_binding" not in settings.async_database_url()

    def test_unrecognised_parameters_survive(self) -> None:
        settings = Settings(  # type: ignore[arg-type]
            database_url="postgresql://u:p@host/db?application_name=x"
        )

        assert "application_name=x" in settings.async_database_url()

    def test_a_url_without_a_query_is_unchanged(self) -> None:
        settings = Settings(database_url="postgresql://u:p@host/db")  # type: ignore[arg-type]

        assert settings.async_database_url() == "postgresql+asyncpg://u:p@host/db"

    def test_the_driver_is_still_rewritten(self) -> None:
        settings = Settings(  # type: ignore[arg-type]
            database_url="postgresql://u:p@host/db?sslmode=require"
        )

        assert settings.async_database_url().startswith("postgresql+asyncpg://")


class TestMcpAllowedHosts:
    """The transport's DNS-rebinding allowlist.

    Getting this wrong rejects every request with 421 while the app starts
    cleanly and every other route works — so it broke the MCP integration suite
    silently once, when a deployment fix was made without re-running it.
    """

    def test_local_hosts_include_the_port_form(self) -> None:
        # A client sends Host as "127.0.0.1:8000". A bare "127.0.0.1" never
        # matches that, and the transport answers 421.
        hosts = Settings().mcp_allowed_hosts  # type: ignore[call-arg]

        assert "127.0.0.1:*" in hosts
        assert "localhost:*" in hosts

    def test_bare_hosts_are_kept_for_deployments(self) -> None:
        # A deployed request arrives on port 443 and Host carries no port.
        hosts = Settings().mcp_allowed_hosts  # type: ignore[call-arg]

        assert "127.0.0.1" in hosts
        assert "localhost" in hosts

    def test_it_can_be_overridden_for_a_deployment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("API_MCP_ALLOWED_HOSTS", '["example.run.app"]')

        assert Settings().mcp_allowed_hosts == ["example.run.app"]  # type: ignore[call-arg]
