"""Team 1 A2A adapter; parsing, selection and plotting live in capabilities."""

import logging
import mimetypes
import os
from typing import Any

from prometheus_api.agent import EventQueue, PrometheusAgent, RequestContext
from prometheus_api.llm_client import LLMConfig
from prometheus_api.storage_client import (
    AsyncStorageClient,
    ConnectionConfig,
    StorageClientError,
)

from capabilities import TableCapabilities


class TablePlotAgent(PrometheusAgent):
    """Dispatch advertised skills and resume SDK conversation follow-ups."""

    @staticmethod
    def _llm_config() -> LLMConfig:
        """Attach the platform proxy key when semantic matching uses LiteLLM."""
        config = LLMConfig.from_env()
        if config.proxy_url:
            config = config.with_api_key(os.getenv("LITELLM_MASTER_KEY"))
        return config

    async def _execute(
        self, params: dict[str, Any], context: RequestContext, event_queue: EventQueue
    ) -> dict:
        """Dispatch one skill after merging validated follow-up answers into saved selectors."""
        skill = params.get("skill_id")
        capabilities = TableCapabilities(self)
        handlers = {
            "team1_extract_table": capabilities._extract,
            "team1_plot_table": capabilities._plot,
            "team1_list_tables": capabilities._list,
            "team1_search_deeplynx": self._search_deeplynx,
            "team1_import_deeplynx_file": self._import_deeplynx_file,
        }
        handler = handlers.get(skill)
        if handler is None:
            return {"status": "error", "message": "Unknown table/plot skill."}
        pending = self.load_context_state(str(context.context_id)).get("pending", {})
        merged = dict(pending) if pending.get("skill_id") == skill else {}
        merged.update(params)
        merged.update(self.get_provided_inputs(context))
        try:
            return await handler(merged, context, event_queue)
        except (ValueError, FileNotFoundError, UnicodeError, StorageClientError) as exc:
            return {"status": "error", "message": str(exc)}

    async def _search_deeplynx(
        self, params: dict, context: RequestContext, events: EventQueue
    ) -> dict:
        """Search DeepLynx and publish the result metadata as a JSON artifact."""
        query = str(params.get("query") or "").strip()
        if not query:
            raise ValueError("Provide a DeepLynx search query.")
        if len(query) > 200:
            raise ValueError("DeepLynx search queries must be 200 characters or fewer.")
        try:
            page_size = int(params.get("page_size") or 20)
        except (TypeError, ValueError):
            raise ValueError("page_size must be an integer between 1 and 100.") from None
        if not 1 <= page_size <= 100:
            raise ValueError("page_size must be between 1 and 100.")

        try:
            config = ConnectionConfig.from_env()
        except ValueError:
            raise ValueError(
                "Configure the agent's local DEEPLYNX_* variables before searching."
            ) from None

        await self.send_status_update(
            context=context,
            event_queue=events,
            message=f"Searching DeepLynx for {query!r}.",
        )
        async with AsyncStorageClient(config) as client:
            page = await client.search.query(query, page=1, page_size=page_size)

        results = [
            {
                "id": item.id,
                "name": item.name,
                "description": item.description,
                "properties": item.properties,
                "file_id": (item.properties or {}).get("file_id"),
                "tags": [tag.name for tag in item.tags],
                "data_source_id": item.data_source_id,
                "created_at": item.created_at.isoformat(),
                "updated_at": item.updated_at.isoformat(),
            }
            for item in page.items
        ]
        payload = {
            "query": query,
            "results": results,
            "result_count": len(results),
            "total_count": page.page_info.total_count,
        }
        artifact_id = await self.send_json_artifact(
            context,
            events,
            payload,
            name="deeplynx_search_results",
            catalog_artifact=True,
        )
        return {"status": "success", **payload, "artifact_id": artifact_id}

    async def _import_deeplynx_file(
        self, params: dict, context: RequestContext, events: EventQueue
    ) -> dict:
        """Register a DeepLynx file as a conversation-scoped artifact reference."""
        raw_file_id = (
            params.get("deeplynx_file_id") or params.get("file_id") or params.get("record_id")
        )
        try:
            file_id = int(raw_file_id)
        except (TypeError, ValueError):
            raise ValueError("Provide a numeric DeepLynx file_id from a search result.") from None
        if file_id <= 0:
            raise ValueError("DeepLynx file_id must be positive.")

        try:
            config = ConnectionConfig.from_env()
        except ValueError:
            raise ValueError(
                "Configure the agent's local DEEPLYNX_* variables before importing files."
            ) from None

        await self.send_status_update(
            context=context,
            event_queue=events,
            message=f"Importing DeepLynx file {file_id} into this conversation.",
        )
        async with AsyncStorageClient(config) as client:
            item = await client.items.get(file_id)
            file_meta = await client.files.get(file_id)

        properties = dict(item.properties or {})
        name = str(
            properties.get("original_filename")
            or item.name
            or file_meta.name
            or f"deeplynx_file_{file_id}"
        )
        mime_type = str(
            properties.get("mime_type")
            or mimetypes.guess_type(name)[0]
            or "application/octet-stream"
        )
        uri = (
            f"deeplynx://organizations/{config.organization_id}"
            f"/projects/{config.project_id}/items/{file_id}"
        )
        reference = await self.send_artifact_reference(
            context=context,
            event_queue=events,
            name=name,
            description=item.description or f"Imported from DeepLynx record {file_id}.",
            uri=uri,
            mime_type=mime_type,
            size_bytes=file_meta.size,
            tags=["deeplynx", "imported"],
            metadata={
                "source": "deeplynx",
                "deeplynx_item_id": file_id,
                "deeplynx_file_id": file_id,
                "original_filename": name,
            },
        )
        return {
            "status": "success",
            "artifact_id": reference.artifact_id,
            "deeplynx_file_id": file_id,
            "name": name,
            "mime_type": mime_type,
            "size_bytes": file_meta.size,
            "message": "The file is now available as a conversation artifact. Pass artifact_id to the table skill.",
        }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    TablePlotAgent(agent_name="Team 1 Table and Plot Agent").run()
