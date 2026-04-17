import os

import requests


DEFAULT_OLLAMA_API_URL = "http://localhost:11434/api/generate"
SUPPORTED_MODEL_API_KINDS = {"ollama", "openai-chat"}


def configured_model_name(default_model):
    value = str(os.getenv("WAGENT_MODEL", os.getenv("OLLAMA_MODEL", default_model))).strip()
    return value or default_model


def configured_model_fallbacks(default_csv):
    raw = os.getenv("WAGENT_MODEL_FALLBACKS", os.getenv("OLLAMA_MODEL_FALLBACKS", default_csv))
    models = []
    for chunk in str(raw or "").split(","):
        clean = chunk.strip()
        if clean and clean not in models:
            models.append(clean)
    return models


def configured_model_api_kind(default_kind="ollama"):
    clean = str(os.getenv("WAGENT_MODEL_API_KIND", default_kind)).strip().lower()
    return clean if clean in SUPPORTED_MODEL_API_KINDS else default_kind


def configured_model_api_url(default_ollama_url=DEFAULT_OLLAMA_API_URL):
    value = str(os.getenv("WAGENT_MODEL_API_URL", os.getenv("OLLAMA_API", default_ollama_url))).strip()
    return value or default_ollama_url


def configured_model_api_key():
    return str(os.getenv("WAGENT_MODEL_API_KEY", os.getenv("OPENAI_API_KEY", ""))).strip()


def _flatten_message_content(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
                continue
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text)
        return "\n".join(part for part in parts if part).strip()
    return ""


def extract_model_response_text(payload):
    if not isinstance(payload, dict):
        raise ValueError("Model API returned a non-dict JSON payload")

    for key in ("response", "output_text", "text", "thinking"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value

    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            message = first.get("message")
            if isinstance(message, dict):
                content = _flatten_message_content(message.get("content"))
                if content:
                    return content
            text = first.get("text")
            if isinstance(text, str) and text.strip():
                return text

    content = _flatten_message_content(payload.get("content"))
    if content:
        return content

    raise ValueError(f"Unsupported model response payload keys: {sorted(payload.keys())}")


def call_model_api(api_kind, api_url, model, prompt, timeout=30, api_key=""):
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    if api_kind == "openai-chat":
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
        }
    else:
        payload = {
            "model": model,
            "prompt": prompt,
            "format": "json",
            "stream": False,
        }

    response = requests.post(api_url, json=payload, headers=headers, timeout=timeout)
    response.raise_for_status()
    return extract_model_response_text(response.json())