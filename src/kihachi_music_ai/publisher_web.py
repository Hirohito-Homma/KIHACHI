"""NiceGUI screen for the Ollama note publisher.

Stdlib-only callers import ``adapters.note_publisher`` instead. This module
imports NiceGUI only when ``serve`` runs, matching how ``intent_llm`` defers its
SDK import until a model is actually called.
"""

from __future__ import annotations

from pathlib import Path

from .adapters.note_publisher import (
    DEFAULT_MODEL,
    OLLAMA_API_URL,
    run_pipeline,
    write_outputs,
)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080


def serve(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    model: str = DEFAULT_MODEL,
    ollama_url: str = OLLAMA_API_URL,
    output_dir: Path | None = None,
) -> None:
    """Open the note publisher in a browser tab."""

    try:
        from nicegui import ui
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise RuntimeError(
            "nicegui is not installed. It is an optional dependency so the core "
            "stays standard-library only (ADR-0001): "
            "pip install 'kihachi-music-ai[publisher]'"
        ) from exc

    out_dir = output_dir or Path("./output")

    @ui.page("/")
    def main_page() -> None:
        ui.label("KIHACHI Music AI - note自動パブリッシャー（Ollama版）").classes(
            "text-2xl font-bold mb-4"
        )

        state = {
            "raw_log": "",
            "model_name": model,
            "is_running": False,
        }

        with ui.row().classes("w-full gap-4"):
            with ui.column().classes("w-1/2"):
                with ui.card().classes("w-full"):
                    ui.label("『設定』").classes("text-lg font-bold")
                    model_input = ui.input(
                        "使用するOllamaモデル名", value=state["model_name"]
                    ).classes("w-full")

                    def set_model(event) -> None:
                        state["model_name"] = event.value

                    model_input.on("change", set_model)

                with ui.card().classes("w-full mt-2"):
                    ui.label("『1. 開発ログの入力』").classes("text-lg font-bold")
                    log_input = ui.textarea(
                        "ObsidianのMarkdownテキストをペースト", value=""
                    ).classes("w-full h-48")

                    async def handle_upload(event) -> None:
                        content = event.content.read().decode("utf-8")
                        log_input.set_value(content)
                        state["raw_log"] = content
                        ui.notify(
                            f"ファイル読み込み完了: {event.name}", type="positive"
                        )

                    ui.upload(
                        label="または .md ファイルをアップロード",
                        on_upload=handle_upload,
                        auto_upload=True,
                    ).classes("w-full")

                    def update_log(event) -> None:
                        state["raw_log"] = event.value

                    log_input.on("change", update_log)

                with ui.card().classes("w-full mt-2"):
                    ui.label("『2. アクション』").classes("text-lg font-bold")
                    btn_auto = ui.button(
                        "4部署を一括自動実行",
                        color="red",
                    ).classes("w-full text-lg py-2")

            with ui.column().classes("w-1/2"):
                ui.label("『ターミナル監視』").classes("text-lg font-bold")
                log_view = ui.log().classes(
                    "w-full h-40 bg-gray-900 text-green-400 p-2 font-mono"
                )
                ui.label("『成果物プレビュー』").classes("text-lg font-bold mt-2")
                with ui.tabs().classes("w-full") as tabs:
                    tab_draft = ui.tab("1. ドラフト")
                    tab_edit = ui.tab("2. note記事")
                    tab_video = ui.tab("3. 動画絵コンテ")
                    tab_pack = ui.tab("4. パッケージ")
                with ui.tab_panels(tabs, value=tab_draft).classes(
                    "w-full h-96 border p-2 overflow-y-auto"
                ):
                    with ui.tab_panel(tab_draft):
                        view_draft = ui.markdown("ドラフト待機中...")
                    with ui.tab_panel(tab_edit):
                        view_edit = ui.markdown("記事待機中...")
                    with ui.tab_panel(tab_video):
                        view_video = ui.markdown("動画絵コンテ待機中...")
                    with ui.tab_panel(tab_pack):
                        view_pack = ui.markdown("最終パッケージ待機中...")

        views = {
            "draft": view_draft,
            "article": view_edit,
            "video": view_video,
            "package": view_pack,
        }

        async def run_ui_pipeline() -> None:
            if not state["raw_log"]:
                ui.notify("開発ログを入力してください", type="warning")
                return

            btn_auto.disable()
            state["is_running"] = True
            log_view.push(">>> パイプラインを始動します（Ollama接続中）")

            async def on_step_start(index: int, department: str, _key: str, _content: str) -> None:
                log_view.push(
                    f">> [{index}/4] {department}: 分析中 ({state['model_name']})..."
                )

            async def on_step_complete(
                index: int, department: str, key: str, content: str
            ) -> None:
                views[key].set_content(content)
                log_view.push(f">> [{index}/4] {department}: 完了")

            try:
                outputs = await run_pipeline(
                    state["raw_log"],
                    model=state["model_name"],
                    api_url=ollama_url,
                    on_step_start=on_step_start,
                    on_step_complete=on_step_complete,
                )
            except ValueError:
                ui.notify("開発ログを入力してください", type="warning")
                btn_auto.enable()
                state["is_running"] = False
                return

            write_outputs(outputs, out_dir)
            log_view.push(
                f">>> すべて完了しました。{out_dir} フォルダに保存されました。"
            )
            ui.notify("全工程が完了し、ファイルを出力しました！", type="positive")
            btn_auto.enable()
            state["is_running"] = False

        btn_auto.on_click(run_ui_pipeline)

    ui.run(
        title="KIHACHI Publisher (Ollama)",
        host=host,
        port=port,
        reload=False,
    )
