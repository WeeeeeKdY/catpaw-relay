/**
 * CatPaw relay provider for pi
 *
 * 本地 relay (127.0.0.1:8789) 已经对外暴露标准 Anthropic Messages 协议，
 * 并在内部完成双通道分流：
 *   - 纯文本 -> catpaw.sankuai.com gateway
 *   - 含工具/tool_result -> catpaw-agent.sankuai.com agent 通道（工具闭环）
 *
 * 所以这个 provider 无需任何协议改写，直接走 pi 内置的
 * streamSimpleAnthropic，把 baseUrl 指到 relay 即可。
 *
 * 安装：已在 ~/.pi/agent/settings.json 的 packages 里。
 * 使用：pi --model catpaw-relay/gpt-6-sol
 *       pi --model catpaw-relay/gpt-5.6-luna
 */

import {
  type SimpleStreamOptions,
  streamSimpleAnthropic,
} from "@earendil-works/pi-ai";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const MODELS = [
  {
    id: "gpt-6.1-sol",
    name: "GPT-6.1 Sol (catpaw relay)",
    contextWindow: 204800,
    maxTokens: 32000,
    input: ["text"],
    reasoning: true,
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
  },
  {
    id: "gpt-6.1-luna",
    name: "GPT-6.1 Luna (catpaw relay)",
    contextWindow: 204800,
    maxTokens: 32000,
    input: ["text"],
    reasoning: true,
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
  },
  {
    id: "gpt-6-sol",
    name: "GPT-6 Sol (catpaw relay)",
    contextWindow: 204800,
    maxTokens: 32000,
    input: ["text"],
    reasoning: true,
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
  },
  {
    id: "gpt-6-luna",
    name: "GPT-6 Luna (catpaw relay)",
    contextWindow: 204800,
    maxTokens: 32000,
    input: ["text"],
    reasoning: true,
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
  },
  {
    id: "gpt-6-astra",
    name: "GPT-6 Astra (catpaw relay)",
    contextWindow: 204800,
    maxTokens: 32000,
    input: ["text"],
    reasoning: true,
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
  },
  {
    id: "gpt-5.6-sol",
    name: "GPT-5.6 Sol (catpaw relay)",
    contextWindow: 204800,
    maxTokens: 32000,
    input: ["text"],
    reasoning: true,
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
  },
  {
    id: "gpt-5.6-luna",
    name: "GPT-5.6 Luna (catpaw relay)",
    contextWindow: 204800,
    maxTokens: 32000,
    input: ["text"],
    reasoning: true,
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
  },
  {
    id: "gpt-5.6-terra",
    name: "GPT-5.6 Terra (catpaw relay)",
    contextWindow: 204800,
    maxTokens: 32000,
    input: ["text"],
    reasoning: true,
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
  },
  {
    id: "deepseek-v4-pro",
    name: "DeepSeek V4 Pro (catpaw relay)",
    contextWindow: 204800,
    maxTokens: 32000,
    input: ["text"],
    reasoning: true,
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
  },
  {
    id: "glm-5.3",
    name: "GLM 5.3 (catpaw relay)",
    contextWindow: 204800,
    maxTokens: 32000,
    input: ["text"],
    reasoning: true,
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
  },
  {
    id: "kimi-k3",
    name: "Kimi K3 (catpaw relay)",
    contextWindow: 204800,
    maxTokens: 32000,
    input: ["text"],
    reasoning: true,
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
  },
  {
    id: "longcat-2.0",
    name: "LongCat 2.0 (catpaw relay)",
    contextWindow: 204800,
    maxTokens: 32000,
    input: ["text"],
    reasoning: true,
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
  },
  {
    id: "minimax-m3",
    name: "MiniMax M3 (catpaw relay)",
    contextWindow: 204800,
    maxTokens: 32000,
    input: ["text"],
    reasoning: true,
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
  },
];

function streamCatpaw(
  model: any,
  context: any,
  options?: SimpleStreamOptions,
) {
  // pi 的 registerProvider 可能丢掉 cost 字段，calculateCost 会读 model.cost.input。
  // 兜底补上 cost，避免 "Cannot read properties of undefined (reading 'input')"。
  const patchedModel = model?.cost
    ? model
    : { ...model, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 } };
  return streamSimpleAnthropic(patchedModel, context, options);
}

export default function (pi: ExtensionAPI) {
  pi.registerProvider("catpaw-relay", {
    name: "CatPaw Relay (Anthropic)",
    api: "anthropic-messages",
    baseUrl: "http://127.0.0.1:8789",
    apiKey: "catpaw-relay",
    streamSimple: streamCatpaw,
    models: MODELS,
  });

  console.error("[catpaw-relay-provider] 已注册 catpaw-relay provider → 127.0.0.1:8789");
}
