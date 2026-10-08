"""
core/ai_handler.py - Unified AI Inference Interface.

Provides a decoupled abstraction layer supporting both cloud REST APIs
(OpenAI-compatible endpoints like OpenRouter, Groq, DeepSeek, OpenAI)
and local offline inference running native llama.cpp binaries inside Termux.
"""

import json
import logging
import os
import re
import subprocess
from typing import Any, Dict, Optional
import requests


class AIHandler:
    """
    Unified AI inference handler supporting both API-based and local execution models.
    """

    def __init__(self, config: Dict[str, Any]) -> None:
        """
        Initialize the AI Handler from system configuration.

        :param config: The 'ai' configuration block from config.json.
        """
        self.config: Dict[str, Any] = config
        self.mode: str = self.config.get("mode", "api").lower()
        self.logger: logging.Logger = logging.getLogger(self.__class__.__name__)

        if self.mode not in ("api", "local"):
            raise ValueError(f"Unsupported AI mode: '{self.mode}'. Must be 'api' or 'local'.")

        self.logger.info(f"AIHandler initialized with mode: {self.mode}")

    def prompt(
        self,
        user_prompt: str,
        system_prompt: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 512
    ) -> str:
        """
        Send a prompt to the configured AI engine and return the textual response.

        :param user_prompt: Main text instruction or question for the model.
        :param system_prompt: Optional persona or instruction constraint.
        :param temperature: Generation randomness (0.0 to 1.0).
        :param max_tokens: Maximum number of tokens to generate.
        :return: Extracted text response from the model.
        """
        if self.mode == "api":
            return self._query_api(user_prompt, system_prompt, temperature, max_tokens)
        else:
            return self._query_local(user_prompt, system_prompt, temperature, max_tokens)

    def _query_api(
        self,
        user_prompt: str,
        system_prompt: Optional[str],
        temperature: float,
        max_tokens: int
    ) -> str:
        """
        Dispatch prompt to an OpenAI-compatible REST API.
        """
        api_config = self.config.get("api", {})
        base_url = api_config.get("base_url", "https://openrouter.ai/api/v1").rstrip("/")
        api_key = api_config.get("api_key", os.getenv("AI_API_KEY", ""))
        model = api_config.get("model", "qwen/qwen-2.5-7b-instruct")
        timeout_seconds = api_config.get("timeout_seconds", 60)

        if not api_key:
            raise ValueError("API Key is missing in configuration (ai.api.api_key) or AI_API_KEY env var.")

        endpoint = f"{base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "r3scue-me/1.0"
        }

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_prompt})

        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens
        }

        self.logger.debug(f"Calling REST API: {endpoint} with model: {model}")
        try:
            response = requests.post(
                endpoint,
                headers=headers,
                data=json.dumps(payload),
                timeout=timeout_seconds
            )
            response.raise_for_status()
            data = response.json()
            generated_content = data["choices"][0]["message"]["content"].strip()
            return generated_content
        except requests.exceptions.RequestException as e:
            self.logger.error(f"HTTP request error during AI API query: {e}")
            raise RuntimeError(f"AI API request failed: {e}") from e
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            self.logger.error(f"Malformed response payload from AI API: {e}")
            raise RuntimeError(f"Unexpected response format from AI API: {e}") from e

    def _query_local(
        self,
        user_prompt: str,
        system_prompt: Optional[str],
        temperature: float,
        max_tokens: int
    ) -> str:
        """
        Execute prompt via native llama.cpp binary in Termux subprocess.
        """
        local_config = self.config.get("local", {})
        binary_path = local_config.get("binary_path", "/data/data/com.termux/files/home/llama.cpp/build/bin/llama-cli")
        model_path = local_config.get("model_path", "")
        threads = local_config.get("threads", 4)
        context_size = local_config.get("context_size", 2048)
        timeout_seconds = local_config.get("timeout_seconds", 180)

        if not os.path.isfile(binary_path):
            raise FileNotFoundError(f"Local llama.cpp binary not found at: {binary_path}")
        if not os.path.isfile(model_path):
            raise FileNotFoundError(f"Model GGUF file not found at: {model_path}")

        # If llama-cli is configured but llama-completion is available in the same
        # directory, prefer llama-completion for clean non-interactive generation.
        effective_bin = binary_path
        if os.path.basename(binary_path) == "llama-cli":
            comp_candidate = os.path.join(os.path.dirname(binary_path), "llama-completion")
            if os.path.isfile(comp_candidate):
                effective_bin = comp_candidate
                self.logger.debug(f"Using headless llama-completion: {effective_bin}")

        # Construct prompt compatible with ChatML / standard instruct formats
        full_prompt = ""
        if system_prompt:
            full_prompt += f"<|im_start|>system\n{system_prompt}<|im_end|>\n"
        full_prompt += f"<|im_start|>user\n{user_prompt}<|im_end|>\n<|im_start|>assistant\n"

        cmd = [
            effective_bin,
            "-m", model_path,
            "-p", full_prompt,
            "-n", str(max_tokens),
            "--temp", str(temperature),
            "-c", str(context_size),
            "-t", str(threads),
            "--no-display-prompt",
            "--log-disable",
        ]

        # For llama-cli: pass -st (--single-turn) so the process terminates
        # immediately upon generating its completion instead of staying in
        # conversational mode waiting at the '>' prompt.
        if "llama-cli" in os.path.basename(effective_bin):
            cmd.append("-st")

        # Strictly eliminate any interactive or conversation mode flags
        # (-i, --interactive, --conversation, -cnv) so llama-cli generates
        # the response and exits immediately rather than waiting at '>'
        forbidden_flags = {
            "-i", "--interactive", "--conversation", "-cnv",
            "--in-prefix", "--in-suffix", "--multiline-input"
        }
        extra_args = local_config.get("extra_args", [])
        if isinstance(extra_args, list):
            clean_extra = [
                str(arg) for arg in extra_args
                if str(arg).strip().lower() not in forbidden_flags
            ]
            cmd.extend(clean_extra)

        self.logger.debug(f"Spawning local llama.cpp process: {' '.join(cmd)}")
        try:
            result = subprocess.run(
                cmd,
                # Absolute input isolation: stdin=DEVNULL prevents blocking on '>' prompt
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                # Absolute error isolation: stderr=DEVNULL completely silences
                # ASCII banners, model load info, and telemetry metrics
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                check=True
            )
            return self._sanitize_local_output(result.stdout, full_prompt=full_prompt)
        except subprocess.TimeoutExpired as e:
            self.logger.error(f"Local llama.cpp inference timed out after {timeout_seconds}s")
            return self.DEFAULT_FALLBACK_TEXT
        except subprocess.CalledProcessError as e:
            self.logger.error(
                f"Local llama.cpp execution failed (exit {e.returncode}). "
                f"Returning clean fallback message."
            )
            return self.DEFAULT_FALLBACK_TEXT
        except Exception as e:
            self.logger.error(f"Unexpected error during local AI inference: {e}")
            return self.DEFAULT_FALLBACK_TEXT

    # Default clean fallback message returned if inference or filtering yields no usable text
    DEFAULT_FALLBACK_TEXT = (
        "No se pudo generar la respuesta de la IA. "
        "Por favor, revisa tus tareas pendientes."
    )

    # Common assistant turn start markers across ChatML/Instruct templates
    ASSISTANT_MARKERS = (
        "<|im_start|>assistant",
        "<|im_start|> assistant",
        "<start_of_turn>model",
        "<|assistant|>",
        "[/INST]",
        "[ASSISTANT]",
    )

    # Common end-of-turn / stop tokens
    STOP_TOKENS = (
        "<|im_end|>",
        "<end_of_turn>",
        "<|eot_id|>",
        "<|end_of_text|>",
        "</s>",
        "<|im_start|>",
    )

    # Diagnostic and telemetry lines that llama-cli / ggml may print
    _DIAGNOSTIC_LINE_RE = re.compile(
        r"^\s*("
        r"\[\s*Prompt:.*t/s.*\]|"
        r"llama_\w+:.*|"
        r"ggml_\w+:.*|"
        r"main:.*|"
        r"(?:llama\.cpp\s+)?build[:\s]\s*\d+.*|"
        r"system_info:.*|"
        r"sampler\s*chain:.*|"
        r"==\s*Running in .*==|"
        r"(?:>\s*)?EOF.*|"
        r"Exiting\.\.\.|"
        r"error:.*|"
        r"warning:.*|"
        r"Loading model\.\.\..*|"
        r"ftype\s*:.*|"
        r"modalities\s*:.*|"
        r"model\s*:.*|"
        r"build\s*:.*|"
        r"available commands:.*|"
        r"/\w+.*|"
        r"={3,}|-{3,}|\*{3,}|"
        r"[▄█▀\s]+"
        r")\s*$",
        re.IGNORECASE,
    )

    @classmethod
    def _sanitize_local_output(cls, raw_output: str, full_prompt: Optional[str] = None) -> str:
        """
        Extract exclusively the assistant's generated response from llama.cpp stdout.

        Performs strict partitioning to discard banners, model initialization info,
        interactive prompts, and echoed prompt blocks. Returns a safe default string
        if the filter yields empty content, ensuring console garbage never leaks to Telegram.
        """
        if not raw_output or not raw_output.strip():
            return cls.DEFAULT_FALLBACK_TEXT

        text = raw_output

        # 1. Start partitioning: strip any prompt echoes and model banners before the response
        if "<|im_start|>assistant" in text:
            text = text.split("<|im_start|>assistant")[-1]
        elif "<|im_start|> assistant" in text:
            text = text.split("<|im_start|> assistant")[-1]
        elif full_prompt and full_prompt in text:
            text = text.split(full_prompt, 1)[-1]
        elif full_prompt and full_prompt.strip() in text:
            text = text.split(full_prompt.strip(), 1)[-1]
        else:
            for marker in cls.ASSISTANT_MARKERS:
                if marker in text:
                    text = text.split(marker)[-1]
                    break

        # 2. If 'available commands:' banner is present (llama-cli chat mode header),
        # drop everything up to the end of the slash commands block.
        if "available commands:" in text:
            after_cmds = text.split("available commands:", 1)[-1]
            cmd_lines = after_cmds.splitlines()
            content_lines = []
            in_banner = True
            for line in cmd_lines:
                stripped = line.strip()
                if in_banner:
                    if not stripped or stripped.startswith("/") or ("/" in stripped and any(c in stripped for c in ("exit", "clear", "read", "glob", "regen"))):
                        continue
                    in_banner = False
                content_lines.append(line)
            text = "\n".join(content_lines)

        # 3. Stop token clipping: discard any subsequent tokens / next turn generations
        for stop_token in cls.STOP_TOKENS:
            if stop_token in text:
                text = text.split(stop_token, 1)[0]

        # 4. Remove residual telemetry, prompt markers, and diagnostic lines
        clean_lines = []
        for line in text.splitlines():
            line_str = line.strip()
            if not line_str or cls._DIAGNOSTIC_LINE_RE.match(line):
                continue
            # Strip leading prompt marker '> ' or '>'
            if line_str.startswith(">"):
                line_str = line_str.lstrip(">").strip()
                if not line_str or cls._DIAGNOSTIC_LINE_RE.match(line_str):
                    continue
            clean_lines.append(line_str)

        final_text = "\n".join(clean_lines).strip()

        # 5. Strict safety fallback: if filter results in empty string, return default text
        if not final_text:
            return cls.DEFAULT_FALLBACK_TEXT

        return final_text


