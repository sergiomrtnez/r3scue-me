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

        # Construct prompt compatible with ChatML / standard instruct formats
        full_prompt = ""
        if system_prompt:
            full_prompt += f"<|im_start|>system\n{system_prompt}<|im_end|>\n"
        full_prompt += f"<|im_start|>user\n{user_prompt}<|im_end|>\n<|im_start|>assistant\n"

        cmd = [
            binary_path,
            "-m", model_path,
            "-p", full_prompt,
            "-n", str(max_tokens),
            "--temp", str(temperature),
            "-c", str(context_size),
            "-t", str(threads),
            "--no-display-prompt",
            # Disable llama.cpp internal logger (model loading, sampler info,
            # perf counters such as "Prompt: 42.1 t/s"). llama-cli has no '-q'.
            "--log-disable",
        ]
        # Optional user-provided flags (e.g. ["-no-cnv"] on builds that default
        # to interactive conversation mode).
        extra_args = local_config.get("extra_args", [])
        if isinstance(extra_args, list):
            cmd.extend(str(arg) for arg in extra_args)

        self.logger.debug(f"Spawning local llama.cpp process: {' '.join(cmd)}")
        try:
            result = subprocess.run(
                cmd,
                # No stdin: if the binary ever drops into interactive mode it
                # receives EOF immediately instead of hanging until timeout.
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                # Telemetry / diagnostics go to stderr: discard them entirely so
                # they can never leak into the AI response or fill a pipe buffer.
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
            raise RuntimeError(f"Local inference timeout: {e}") from e
        except subprocess.CalledProcessError as e:
            # stderr is discarded by design; run the command manually to debug.
            self.logger.error(
                f"Local llama.cpp execution failed (exit {e.returncode}). "
                f"Run the binary manually in Termux to inspect diagnostics."
            )
            raise RuntimeError(f"Local llama.cpp execution error (exit {e.returncode})") from e

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
        r"build:\s*\d+.*|"
        r"system_info:.*|"
        r"sampler\s*chain:.*|"
        r">\s*EOF.*|"
        r"Exiting\.\.\."
        r")\s*$",
        re.IGNORECASE,
    )

    @classmethod
    def _sanitize_local_output(cls, raw_output: str, full_prompt: Optional[str] = None) -> str:
        """
        Extract exclusively the assistant's generated response from llama.cpp stdout.

        Discards ASCII banners, system diagnostics, and prompt echoes that precede
        the assistant generation token, and removes trailing stop tokens or telemetry lines.
        """
        if not raw_output:
            return ""

        text = raw_output

        # 1. If full prompt is present in raw output (prompt echo), discard everything up to and including it
        if full_prompt and full_prompt in text:
            text = text.split(full_prompt, 1)[-1]
        elif full_prompt and full_prompt.strip() in text:
            text = text.split(full_prompt.strip(), 1)[-1]
        else:
            # 2. Check for assistant turn markers (e.g. ChatML '<|im_start|>assistant')
            # If present, everything preceding the last marker is banner/prompt echo.
            for marker in cls.ASSISTANT_MARKERS:
                if marker in text:
                    text = text.split(marker)[-1]
                    break

        # 3. Discard trailing stop tokens and subsequent generation turns
        for stop_token in cls.STOP_TOKENS:
            if stop_token in text:
                text = text.split(stop_token, 1)[0]

        # 4. Remove residual telemetry and diagnostic lines
        clean_lines = []
        for line in text.splitlines():
            if not cls._DIAGNOSTIC_LINE_RE.match(line):
                clean_lines.append(line)

        return "\n".join(clean_lines).strip()

