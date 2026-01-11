"""
LLuna v6.0 LLM Module
=====================
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Dict, Any, Optional
from enum import Enum
import json
import logging
import requests
import time

logger = logging.getLogger(__name__)


class ConnectionStatus(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"


@dataclass
class Message:
    role: str
    content: str


@dataclass
class LLMResponse:
    content: str
    finish_reason: str = "stop"
    tokens_used: int = 0
    latency_ms: int = 0


class BaseLLMProvider(ABC):
    def __init__(self, base_url: str, model: str, **kwargs):
        self.base_url = base_url.rstrip('/')
        self.model = model
        self.timeout = kwargs.get('timeout', 120)
        self.temperature = kwargs.get('temperature', 0.7)
        self.max_tokens = kwargs.get('max_tokens', 2048)
        self.status = ConnectionStatus.DISCONNECTED
        self.error_message = ""
        self.available_models: List[str] = []
        self.last_latency = 0
    
    @abstractmethod
    def chat(self, messages, stream=False) -> LLMResponse:
        pass
    
    @abstractmethod
    def connect(self) -> bool:
        pass
    
    @abstractmethod
    def disconnect(self):
        pass
    
    @abstractmethod
    def is_available(self) -> bool:
        pass
    
    @abstractmethod
    def get_models(self) -> List[str]:
        pass
    
    def set_model(self, model: str):
        self.model = model
    
    def get_status(self) -> Dict[str, Any]:
        return {
            "status": self.status.value,
            "model": self.model,
            "base_url": self.base_url,
            "error": self.error_message,
            "available_models": self.available_models,
            "last_latency_ms": self.last_latency
        }
    
    @property
    def is_connected(self) -> bool:
        return self.status == ConnectionStatus.CONNECTED


class OllamaProvider(BaseLLMProvider):
    def __init__(self, base_url: str = "http://localhost:11434", model: str = "llama3.2", **kwargs):
        super().__init__(base_url, model, **kwargs)
        self.keep_alive = kwargs.get('keep_alive', '10m')
    
    def connect(self) -> bool:
        self.status = ConnectionStatus.CONNECTING
        try:
            r = requests.get(f"{self.base_url}/api/tags", timeout=10)
            if r.status_code == 200:
                self.available_models = [m["name"] for m in r.json().get("models", [])]
                if self.model not in self.available_models and self.available_models:
                    self.model = self.available_models[0]
                self.status = ConnectionStatus.CONNECTED
                self.error_message = ""
                return True
            self.status = ConnectionStatus.ERROR
            self.error_message = f"HTTP {r.status_code}"
            return False
        except requests.exceptions.ConnectionError:
            self.status = ConnectionStatus.ERROR
            self.error_message = "Cannot connect to Ollama"
            return False
        except Exception as e:
            self.status = ConnectionStatus.ERROR
            self.error_message = str(e)
            return False
    
    def disconnect(self):
        self.status = ConnectionStatus.DISCONNECTED
    
    def is_available(self) -> bool:
        try:
            return requests.get(f"{self.base_url}/api/tags", timeout=5).status_code == 200
        except:
            return False
    
    def get_models(self) -> List[str]:
        try:
            return [m["name"] for m in requests.get(f"{self.base_url}/api/tags", timeout=10).json().get("models", [])]
        except:
            return []
    
    def chat(self, messages: List[Message], stream=False) -> LLMResponse:
        formatted = [{"role": m.role, "content": m.content} for m in messages]
        payload = {
            "model": self.model,
            "messages": formatted,
            "stream": False,
            "keep_alive": self.keep_alive,
            "options": {
                "temperature": self.temperature,
                "num_predict": self.max_tokens,
                "num_ctx": 4096,
                "repeat_penalty": 1.1,
            }
        }
        
        start = time.time()
        try:
            r = requests.post(f"{self.base_url}/api/chat", json=payload, timeout=self.timeout)
            self.last_latency = int((time.time() - start) * 1000)
            
            if r.status_code == 200:
                data = r.json()
                return LLMResponse(
                    content=data.get("message", {}).get("content", ""),
                    tokens_used=data.get("eval_count", 0),
                    latency_ms=self.last_latency
                )
            return LLMResponse(content=f"Error: HTTP {r.status_code}", finish_reason="error")
        except requests.exceptions.Timeout:
            return LLMResponse(content="Error: Timeout", finish_reason="error")
        except Exception as e:
            return LLMResponse(content=f"Error: {e}", finish_reason="error")


class LMStudioProvider(BaseLLMProvider):
    def __init__(self, base_url: str = "http://localhost:1234", model: str = "local-model", **kwargs):
        super().__init__(base_url, model, **kwargs)
    
    def connect(self) -> bool:
        self.status = ConnectionStatus.CONNECTING
        try:
            r = requests.get(f"{self.base_url}/v1/models", timeout=10)
            if r.status_code == 200:
                self.available_models = [m.get("id", "local-model") for m in r.json().get("data", [])] or ["local-model"]
                if self.model not in self.available_models:
                    self.model = self.available_models[0]
                self.status = ConnectionStatus.CONNECTED
                self.error_message = ""
                return True
            self.status = ConnectionStatus.ERROR
            self.error_message = f"HTTP {r.status_code}"
            return False
        except requests.exceptions.ConnectionError:
            self.status = ConnectionStatus.ERROR
            self.error_message = "Cannot connect to LM Studio"
            return False
        except Exception as e:
            self.status = ConnectionStatus.ERROR
            self.error_message = str(e)
            return False
    
    def disconnect(self):
        self.status = ConnectionStatus.DISCONNECTED
    
    def is_available(self) -> bool:
        try:
            return requests.get(f"{self.base_url}/v1/models", timeout=5).status_code == 200
        except:
            return False
    
    def get_models(self) -> List[str]:
        try:
            return [m.get("id", "local-model") for m in requests.get(f"{self.base_url}/v1/models", timeout=10).json().get("data", [])] or ["local-model"]
        except:
            return ["local-model"]
    
    def chat(self, messages: List[Message], stream=False) -> LLMResponse:
        formatted = [{"role": m.role, "content": m.content} for m in messages]
        payload = {
            "model": self.model,
            "messages": formatted,
            "stream": False,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        
        start = time.time()
        try:
            r = requests.post(f"{self.base_url}/v1/chat/completions", json=payload, timeout=self.timeout)
            self.last_latency = int((time.time() - start) * 1000)
            
            if r.status_code == 200:
                data = r.json()
                choices = data.get("choices", [])
                content = choices[0].get("message", {}).get("content", "") if choices else ""
                return LLMResponse(
                    content=content,
                    tokens_used=data.get("usage", {}).get("total_tokens", 0),
                    latency_ms=self.last_latency
                )
            return LLMResponse(content=f"Error: HTTP {r.status_code}", finish_reason="error")
        except Exception as e:
            return LLMResponse(content=f"Error: {e}", finish_reason="error")


class LLMManager:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.providers: Dict[str, BaseLLMProvider] = {}
        self.current_provider: Optional[str] = None
        self._init_providers()
    
    def _init_providers(self):
        llm = self.config.get("llm", {})
        agent = self.config.get("agent", {})
        
        o = llm.get("ollama", {})
        self.providers["ollama"] = OllamaProvider(
            base_url=o.get("base_url", "http://localhost:11434"),
            model=o.get("default_model", "llama3.2"),
            temperature=agent.get("temperature", 0.7),
            max_tokens=agent.get("max_tokens", 2048),
            timeout=agent.get("timeout", 120),
        )
        
        l = llm.get("lmstudio", {})
        self.providers["lmstudio"] = LMStudioProvider(
            base_url=l.get("base_url", "http://localhost:1234"),
            model=l.get("default_model", "local-model"),
            temperature=agent.get("temperature", 0.7),
            max_tokens=agent.get("max_tokens", 2048),
            timeout=agent.get("timeout", 120)
        )
    
    def connect(self, provider: str, model: Optional[str] = None) -> bool:
        if provider not in self.providers:
            return False
        if self.current_provider:
            self.disconnect()
        
        p = self.providers[provider]
        if model:
            p.set_model(model)
        
        if p.connect():
            self.current_provider = provider
            return True
        return False
    
    def disconnect(self):
        if self.current_provider:
            self.providers[self.current_provider].disconnect()
            self.current_provider = None
    
    @property
    def provider(self) -> Optional[BaseLLMProvider]:
        return self.providers.get(self.current_provider) if self.current_provider else None
    
    @property
    def is_connected(self) -> bool:
        return self.provider is not None and self.provider.is_connected
    
    def chat(self, messages: List[Message], stream=False) -> LLMResponse:
        if not self.is_connected:
            return LLMResponse(content="Not connected", finish_reason="error")
        return self.provider.chat(messages, stream)
    
    def get_status(self) -> Dict[str, Any]:
        return {
            "current_provider": self.current_provider,
            "providers": {n: p.get_status() for n, p in self.providers.items()}
        }
    
    def get_provider_info(self) -> List[Dict[str, Any]]:
        return [{
            "name": n,
            "available": p.is_available(),
            "connected": p.is_connected,
            "model": p.model,
            "models": p.available_models or p.get_models(),
        } for n, p in self.providers.items()]
