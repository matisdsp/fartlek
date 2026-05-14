"""CoachingService — orchestrates the Claude tool use loop.

Stateless across requests: receives full conversation history from the client,
runs the LLM-tool loop, returns updated history. Persistence (DB-backed
Conversation repository) is a phase-2 concern.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Final

from src.coaching.adapters.tools.registry import ToolRegistry
from src.coaching.domain import (
    Conversation,
    Message,
    ToolResultPart,
    ToolUsePart,
)
from src.coaching.ports import LLMPort

log = logging.getLogger(__name__)

SYSTEM_PROMPT_FR: Final[str] = """Tu es un coach sportif et santé personnel IA, expert en physiologie de l'effort, planification d'entraînement, sommeil et récupération.

Tu as accès aux données Garmin réelles de l'utilisateur via des outils. **Utilise systématiquement les outils** pour fonder tes réponses sur ses données, jamais sur des généralités. Si une question concerne sa forme, son entraînement, son sommeil, sa récupération — appelle au moins un outil avant de répondre.

Date du jour : {today}.

Règles :
- Réponds en français, ton direct, concis, actionnable.
- Cite les chiffres réels (sommeil X h, FC repos Y, HRV Z) quand pertinent.
- Si tu fais une recommandation, explique brièvement le « pourquoi » basé sur les données observées.
- Pour analyser une activité spécifique, utilise d'abord get_recent_activities pour trouver l'activity_id, puis get_activity_details pour le détail.
- Si un outil échoue (clé "error" dans le résultat), continue avec les autres outils disponibles et signale-le brièvement.
- N'invente jamais de données. Si l'info manque, dis-le."""

MAX_TOOL_ITERATIONS: Final[int] = 10


class CoachingService:
    def __init__(self, llm: LLMPort, tools: ToolRegistry):
        self._llm = llm
        self._tools = tools

    async def handle_messages(self, messages: list[Message]) -> Conversation:
        """Run the tool use loop until Claude stops calling tools.

        Caller passes the full conversation history (we don't persist).
        We append assistant turns + tool result turns and return the
        complete conversation.
        """
        conv = Conversation(messages=list(messages))
        system = SYSTEM_PROMPT_FR.format(today=date.today().isoformat())

        for iteration in range(MAX_TOOL_ITERATIONS):
            response = await self._llm.complete(
                system=system,
                messages=conv.messages,
                tools=self._tools.schemas(),
            )

            conv.add_assistant_raw(response.raw_assistant_content)

            if response.stop_reason != "tool_use":
                if iteration > 0:
                    log.info("Tool loop ended after %d iterations", iteration + 1)
                break

            tool_calls = [p for p in response.content_parts if isinstance(p, ToolUsePart)]
            if not tool_calls:
                log.warning("stop_reason=tool_use but no tool_use parts found")
                break

            results: list[ToolResultPart] = []
            for call in tool_calls:
                log.info("Tool call: %s(%s)", call.name, call.input)
                payload, is_error = await self._tools.execute(call.name, call.input)
                results.append(
                    ToolResultPart(
                        type="tool_result",
                        tool_use_id=call.id,
                        content=payload,
                        is_error=is_error,
                    )
                )

            conv.add_tool_results(results)
        else:
            log.warning("Tool loop hit MAX_TOOL_ITERATIONS=%d", MAX_TOOL_ITERATIONS)

        return conv
