#!/usr/bin/env python3
"""记忆沙箱本地 Web UI（不依赖 tkinter，兼容 macOS 26）。"""

from __future__ import annotations

import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import MemorySandbox
from core.config import load_config
from core.paths import app_support_dir, default_config_path, default_persist_dir, is_frozen

HOST = "127.0.0.1"
PREFERRED_PORT = 8765
# 前端/协议版本：用于识别旧进程（无思考过程流）并提示重启
UI_BUILD = "20260731-stream2"
UI_FEATURES = ("chat_stream", "think_card")

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>记忆沙箱</title>
<style>
  :root {
    --bg: #f4efe6;
    --ink: #1f2a24;
    --panel: #fffaf3;
    --accent: #2f6f57;
    --muted: #6d7a72;
    --line: #ddd2c2;
    --side: #f8f4ec;
    --danger: #9a3f2c;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: "PingFang SC", "Hiragino Sans GB", "Avenir Next", sans-serif;
    background:
      radial-gradient(1200px 600px at 10% -10%, #e7f0e8 0%, transparent 55%),
      radial-gradient(900px 500px at 100% 0%, #efe4d2 0%, transparent 50%),
      var(--bg);
    color: var(--ink);
    min-height: 100vh;
  }
  .shell {
    display: grid;
    grid-template-columns: 320px 1fr;
    min-height: 100vh;
  }
  @media (max-width: 900px) {
    .shell { grid-template-columns: 1fr; }
    .sidebar { max-height: 40vh; border-right: none; border-bottom: 1px solid var(--line); }
  }
  .sidebar {
    background: var(--side);
    border-right: 1px solid var(--line);
    display: flex;
    flex-direction: column;
    padding: 18px 14px;
    overflow: hidden;
  }
  .side-title {
    font-size: 15px;
    font-weight: 700;
    margin: 0 0 4px;
  }
  .side-sub { color: var(--muted); font-size: 12px; margin-bottom: 12px; }
  .side-tabs { display: flex; gap: 6px; margin-bottom: 10px; }
  .side-tabs button {
    flex: 1;
    border: 1px solid var(--line);
    background: #fff;
    border-radius: 8px;
    padding: 6px 8px;
    font-size: 12px;
    cursor: pointer;
  }
  .side-tabs button.active {
    background: var(--accent);
    border-color: var(--accent);
    color: #fff;
  }
  #qaList {
    flex: 1;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding-right: 2px;
  }
  .qa-item {
    position: relative;
    background: #fff;
    border: 1px solid var(--line);
    border-radius: 12px;
    padding: 10px 12px;
    cursor: pointer;
    transition: border-color .15s, box-shadow .15s;
  }
  .qa-item:hover { border-color: var(--accent); box-shadow: 0 2px 10px rgba(47,111,87,.08); }
  .qa-item.pending { border-style: dashed; background: #fffdf8; }
  .qa-top {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    margin-bottom: 6px;
  }
  .qa-del {
    border: 1px solid #e2b4aa;
    background: #fff;
    color: var(--danger);
    border-radius: 8px;
    padding: 2px 8px;
    font-size: 11px;
    cursor: pointer;
    flex-shrink: 0;
  }
  .qa-del:hover { background: #fff1ee; }
  .qa-q {
    font-size: 13px;
    font-weight: 600;
    margin: 0 0 4px;
    word-break: break-word;
  }
  .qa-a {
    font-size: 12px;
    color: var(--muted);
    margin: 0;
    word-break: break-word;
    white-space: pre-wrap;
  }
  .qa-a.md {
    white-space: normal;
    max-height: none;
    overflow: visible;
    line-height: 1.55;
    color: #3d4a42;
  }
  .qa-a.md p { margin: 0 0 6px; }
  .qa-a.md ul, .qa-a.md ol { margin: 4px 0 8px; padding-left: 1.2em; }
  .qa-a.md li { margin: 2px 0; }
  .qa-a.md code {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 11px;
    background: #eef3f0;
    padding: 1px 4px;
    border-radius: 4px;
  }
  .qa-badge {
    display: inline-block;
    font-size: 11px;
    padding: 1px 6px;
    border-radius: 999px;
    background: #e8f2ec;
    color: var(--accent);
  }
  .qa-badge.pending-badge { background: #f7ebe0; color: #9a6230; }
  .main {
    display: flex;
    flex-direction: column;
    padding: 22px 18px 18px;
    min-width: 0;
  }
  header {
    display: flex;
    align-items: baseline;
    gap: 14px;
    margin-bottom: 12px;
  }
  h1 { margin: 0; font-size: 26px; }
  .sub { color: var(--muted); font-size: 13px; }
  .toolbar {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-bottom: 12px;
    align-items: center;
  }
  .agent-mode-label {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-size: 12px;
    color: var(--muted);
    border: 1px solid var(--line);
    background: #fff;
    border-radius: 10px;
    padding: 4px 8px 4px 10px;
  }
  .agent-mode-label select {
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 4px 8px;
    font-size: 12px;
    background: #f7f8f6;
    color: var(--ink);
    cursor: pointer;
  }
  button {
    border: 1px solid var(--line);
    background: #fff;
    color: var(--ink);
    border-radius: 10px;
    padding: 8px 12px;
    font-size: 13px;
    cursor: pointer;
  }
  button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
  button.danger { border-color: #c47b6a; color: var(--danger); }
  button:disabled { opacity: 0.55; cursor: wait; }
  #chat {
    flex: 1;
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: 16px;
    padding: 16px;
    overflow-y: auto;
    min-height: 280px;
  }
  .msg { margin: 0 0 14px; line-height: 1.55; white-space: pre-wrap; word-break: break-word; }
  .msg.md {
    white-space: normal;
    line-height: 1.65;
  }
  .msg.md p { margin: 0 0 10px; }
  .msg.md p:last-child { margin-bottom: 0; }
  .msg.md ul, .msg.md ol { margin: 6px 0 12px; padding-left: 1.35em; }
  .msg.md li { margin: 4px 0; }
  .msg.md strong { font-weight: 700; color: #16352a; }
  .msg.md code {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 12.5px;
    background: #eef3f0;
    padding: 1px 5px;
    border-radius: 4px;
  }
  .msg.md pre {
    background: #f3f6f4;
    border: 1px solid var(--line);
    border-radius: 10px;
    padding: 10px 12px;
    overflow-x: auto;
    margin: 8px 0 12px;
  }
  .msg.md pre code { background: none; padding: 0; font-size: 12.5px; }
  .msg.md h1, .msg.md h2, .msg.md h3 {
    margin: 12px 0 8px;
    font-size: 15px;
    font-weight: 700;
    color: #16352a;
  }
  .user { color: #0f4c3a; font-weight: 600; }
  .bot { color: var(--ink); }
  .bot-label {
    display: inline-block;
    font-size: 11px;
    font-weight: 700;
    color: var(--accent);
    background: #e8f2ec;
    border-radius: 6px;
    padding: 1px 7px;
    margin-bottom: 8px;
  }
  .meta { color: var(--muted); font-size: 12px; margin-top: -8px; margin-bottom: 14px; }
  .sys { color: #7a6a4f; }

  /* 思考过程（对齐 CLI 阶段进度） */
  .think-card {
    margin: 0 0 14px;
    border: 1px solid #d5e4db;
    background: linear-gradient(180deg, #f7fbf8 0%, #f2f7f4 100%);
    border-radius: 14px;
    padding: 12px 14px 10px;
    box-shadow: 0 2px 10px rgba(47,111,87,.06);
  }
  .think-head {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 13px;
    font-weight: 700;
    color: var(--accent);
    margin-bottom: 8px;
  }
  .think-head .elapsed {
    margin-left: auto;
    font-weight: 500;
    font-size: 12px;
    color: var(--muted);
    font-variant-numeric: tabular-nums;
  }
  .think-spinner {
    width: 14px; height: 14px;
    border: 2px solid #c5ddd0;
    border-top-color: var(--accent);
    border-radius: 50%;
    animation: think-spin .7s linear infinite;
    flex-shrink: 0;
  }
  .think-card.done .think-spinner {
    animation: none;
    border-color: var(--accent);
    background: var(--accent);
    box-shadow: inset 0 0 0 2px #f7fbf8;
    position: relative;
  }
  .think-card.done .think-spinner::after {
    content: "";
    position: absolute;
    left: 3px; top: 1px;
    width: 4px; height: 7px;
    border: solid #fff;
    border-width: 0 2px 2px 0;
    transform: rotate(45deg);
  }
  .think-card.error .think-head { color: var(--danger); }
  .think-steps {
    list-style: none;
    margin: 0;
    padding: 0;
    max-height: 220px;
    overflow-y: auto;
  }
  .think-steps li {
    display: flex;
    gap: 8px;
    align-items: flex-start;
    font-size: 12.5px;
    line-height: 1.45;
    color: #4a5c52;
    padding: 4px 0;
    border-top: 1px dashed #e2ebe5;
  }
  .think-steps li:first-child { border-top: none; }
  .think-steps li .mark {
    flex-shrink: 0;
    width: 14px;
    text-align: center;
    color: var(--muted);
    font-size: 11px;
    margin-top: 1px;
  }
  .think-steps li.done .mark { color: var(--accent); }
  .think-steps li.active {
    color: #16352a;
    font-weight: 600;
  }
  .think-steps li.active .mark { color: var(--accent); }
  @keyframes think-spin { to { transform: rotate(360deg); } }
  .composer-wrap { position: relative; margin-top: 12px; }
  .mode-bar {
    display: none;
    align-items: center;
    gap: 8px;
    margin-bottom: 8px;
    font-size: 12px;
    color: var(--accent);
  }
  .mode-bar.show { display: flex; }
  .mode-bar .cancel {
    border: none;
    background: transparent;
    color: var(--danger);
    padding: 0 4px;
    font-size: 12px;
  }
  .composer {
    display: grid;
    grid-template-columns: 1fr auto;
    gap: 10px;
  }
  textarea {
    width: 100%;
    min-height: 72px;
    resize: vertical;
    border-radius: 12px;
    border: 1px solid var(--line);
    padding: 12px;
    font: inherit;
    background: #fff;
  }
  footer { margin-top: 10px; color: var(--muted); font-size: 12px; }

  /* slash menu */
  .slash-menu {
    display: none;
    position: absolute;
    left: 0;
    bottom: calc(100% + 8px);
    width: min(420px, 100%);
    background: #fff;
    border: 1px solid var(--line);
    border-radius: 14px;
    box-shadow: 0 12px 40px rgba(31,42,36,.12);
    z-index: 20;
    overflow: hidden;
  }
  .slash-menu.show { display: block; }
  .slash-head {
    padding: 10px 14px;
    font-size: 12px;
    color: var(--muted);
    border-bottom: 1px solid var(--line);
  }
  .slash-item {
    display: flex;
    gap: 10px;
    padding: 10px 14px;
    cursor: pointer;
    border: none;
    background: transparent;
    width: 100%;
    text-align: left;
    border-radius: 0;
  }
  .slash-item:hover, .slash-item.active { background: #f0f7f3; }
  .slash-ico {
    width: 28px; height: 28px; border-radius: 8px;
    background: #e8f2ec; color: var(--accent);
    display: flex; align-items: center; justify-content: center;
    font-size: 12px; font-weight: 700; flex-shrink: 0;
  }
  .slash-name { font-size: 13px; font-weight: 600; }
  .slash-desc { font-size: 12px; color: var(--muted); margin-top: 2px; }

  /* modal */
  .modal-mask {
    display: none;
    position: fixed; inset: 0;
    background: rgba(31,42,36,.42);
    backdrop-filter: blur(3px);
    z-index: 50;
    align-items: center;
    justify-content: center;
    padding: 28px 20px;
  }
  .modal-mask.show { display: flex; }
  .modal {
    width: min(560px, 96vw);
    background: #fff;
    border-radius: 18px;
    border: 1px solid var(--line);
    box-shadow: 0 24px 64px rgba(20,40,30,.18);
    padding: 22px 24px 18px;
  }
  .modal.modal-answer {
    width: min(860px, 96vw);
    max-height: min(90vh, 920px);
    display: flex;
    flex-direction: column;
    padding: 26px 28px 20px;
  }
  .modal h3 {
    margin: 0 0 6px;
    font-size: 20px;
    font-weight: 700;
    letter-spacing: 0.01em;
    color: var(--ink);
  }
  .modal .modal-hint {
    margin: 0 0 14px;
    font-size: 13px;
    line-height: 1.45;
    color: var(--muted);
  }
  .modal .qbox {
    background: #f4f7f5;
    border: 1px solid var(--line);
    border-radius: 12px;
    padding: 12px 14px;
    font-size: 14px;
    line-height: 1.55;
    margin-bottom: 14px;
    white-space: pre-wrap;
    word-break: break-word;
    max-height: 22vh;
    overflow-y: auto;
    flex-shrink: 0;
  }
  .modal .qbox::before {
    content: "问";
    display: inline-block;
    margin-right: 8px;
    padding: 1px 7px;
    border-radius: 6px;
    background: var(--accent);
    color: #fff;
    font-size: 11px;
    font-weight: 700;
    vertical-align: 1px;
  }
  .modal textarea {
    min-height: 120px;
    margin-bottom: 14px;
  }
  .answer-toolbar {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 10px;
    flex-wrap: wrap;
  }
  .answer-tabs {
    display: inline-flex;
    border: 1px solid var(--line);
    border-radius: 10px;
    overflow: hidden;
    background: #f3f6f4;
  }
  .answer-tabs button {
    border: none;
    border-radius: 0;
    background: transparent;
    padding: 6px 14px;
    font-size: 12px;
    color: var(--muted);
  }
  .answer-tabs button.active {
    background: #fff;
    color: var(--accent);
    font-weight: 700;
    box-shadow: inset 0 0 0 1px var(--line);
  }
  .answer-toolbar .ghost {
    border: 1px dashed var(--line);
    background: #fff;
    color: var(--accent);
    font-size: 12px;
    padding: 6px 12px;
  }
  .answer-toolbar .ghost:hover { background: #f0f7f3; }
  .answer-panes {
    flex: 1 1 auto;
    min-height: 320px;
    max-height: 52vh;
    display: flex;
    flex-direction: column;
    margin-bottom: 16px;
  }
  .modal.modal-answer textarea {
    flex: 1 1 auto;
    min-height: 300px;
    height: 100%;
    margin-bottom: 0;
    font-size: 14.5px;
    line-height: 1.75;
    padding: 14px 16px;
    border-radius: 12px;
    border: 1px solid var(--line);
    background: #fbfcfb;
    resize: vertical;
    font-family: "SF Pro Text", "PingFang SC", "Helvetica Neue", sans-serif;
    tab-size: 2;
  }
  .modal.modal-answer textarea:focus {
    outline: none;
    border-color: var(--accent);
    background: #fff;
    box-shadow: 0 0 0 3px rgba(47,111,87,.14);
  }
  .answer-preview {
    flex: 1 1 auto;
    min-height: 300px;
    overflow-y: auto;
    padding: 16px 18px;
    border-radius: 12px;
    border: 1px solid var(--line);
    background: #fff;
    font-size: 14.5px;
    line-height: 1.7;
    color: var(--ink);
  }
  .answer-preview p { margin: 0 0 10px; }
  .answer-preview ul, .answer-preview ol { margin: 6px 0 12px; padding-left: 1.4em; }
  .answer-preview li { margin: 4px 0; }
  .answer-preview strong { color: #16352a; }
  .answer-preview code {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 12.5px;
    background: #eef3f0;
    padding: 1px 5px;
    border-radius: 4px;
  }
  .answer-preview pre {
    background: #f3f6f4;
    border: 1px solid var(--line);
    border-radius: 10px;
    padding: 10px 12px;
    overflow-x: auto;
    margin: 8px 0 12px;
  }
  .answer-preview.hidden, .answer-panes textarea.hidden { display: none; }
  .modal-actions {
    display: flex;
    justify-content: flex-end;
    gap: 10px;
    flex-wrap: wrap;
    flex-shrink: 0;
    padding-top: 2px;
  }
  .modal-actions button { min-width: 88px; padding: 9px 16px; }
  .modal .warn {
    font-size: 14px;
    color: #7a3b2e;
    line-height: 1.55;
    margin: 0 0 14px;
  }
  .modal label.check {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 14px;
    margin-bottom: 16px;
    color: var(--ink);
    cursor: pointer;
  }
  .modal label.check input { width: auto; }
  .modal-actions .danger {
    background: var(--danger);
    color: #fff;
    border-color: var(--danger);
  }
  .tag-filter {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    padding: 0 0 10px;
  }
  .tag-chip {
    border: 1px solid var(--line);
    background: #fff;
    color: var(--muted);
    border-radius: 999px;
    padding: 3px 10px;
    font-size: 12px;
    cursor: pointer;
  }
  .tag-chip.active {
    background: var(--ink);
    color: #fff;
    border-color: var(--ink);
  }
  .meta-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 8px 10px;
    margin: 10px 0 12px;
  }
  .meta-grid label {
    display: flex;
    flex-direction: column;
    gap: 4px;
    font-size: 12px;
    color: var(--muted);
  }
  .meta-grid label.span2 { grid-column: 1 / -1; }
  .meta-grid input, .meta-grid select {
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 7px 9px;
    font-size: 13px;
    color: var(--ink);
    background: #fff;
  }
  .qa-badge.kind { background: #e8eef8; color: #2a4a7a; }
  @media (max-width: 640px) {
    .modal.modal-answer {
      width: 100%;
      max-height: 94vh;
      padding: 18px 16px 14px;
    }
    .modal.modal-answer textarea { min-height: 220px; font-size: 14px; }
  }
</style>
</head>
<body>
  <div class="shell">
    <aside class="sidebar">
      <div class="side-title">记忆清单（问）</div>
      <div class="side-sub">点选可补全/修改答案；点「删除」移除单条</div>
      <div class="side-tabs">
        <button type="button" class="active" data-tab="pending" id="tabPending">待补全答</button>
        <button type="button" data-tab="saved" id="tabSaved">已记住</button>
      </div>
      <div class="tag-filter" id="tagFilter" hidden></div>
      <div id="qaList"></div>
    </aside>

    <div class="main">
      <header>
        <h1>记忆沙箱</h1>
        <div class="sub">输入 / 选择指令 · 原有文字指令仍可用</div>
      </header>
      <div class="toolbar">
        <label class="agent-mode-label" title="本地 Cursor LLM 回退权限：Ask 只读 / Plan 规划 / Agent 可写">
          Agent 模式
          <select id="agentMode">
            <option value="ask">Ask 只读</option>
            <option value="plan">Plan 规划</option>
            <option value="agent">Agent 可写</option>
          </select>
        </label>
        <button type="button" id="btnWorking">查看短时记忆</button>
        <button type="button" id="btnLong">查看长时记忆</button>
        <button type="button" id="btnAllMem">查看全部记忆</button>
        <button type="button" id="btnStatus">记忆状态</button>
        <button type="button" id="btnClear">清空工作记忆</button>
        <button type="button" id="btnBackupLong">备份长时记忆</button>
        <button type="button" id="btnExportPack">导出知识包</button>
        <button type="button" id="btnGitCheck">检查过时记忆</button>
        <button type="button" id="btnArchive">归档陈旧记忆</button>
        <button type="button" class="danger" id="btnClearLong">清空长时记忆</button>
        <button type="button" id="btnSeed">写入开发种子</button>
        <button type="button" id="btnData">打开数据目录</button>
        <button type="button" class="danger" id="btnQuit">退出应用</button>
      </div>
      <div id="chat"></div>
      <div class="composer-wrap">
        <div class="mode-bar" id="modeBar">
          <span>当前指令：记忆 — 请输入「问」</span>
          <button type="button" class="cancel" id="btnCancelMode">取消</button>
        </div>
        <div class="slash-menu" id="slashMenu">
          <div class="slash-head">Commands · 输入过滤</div>
          <div id="slashItems"></div>
        </div>
        <div class="composer">
          <textarea id="input" placeholder="输入问题，或输入 / 选择指令。Enter 发送，Shift+Enter 换行"></textarea>
          <button class="primary" type="button" id="btnSend">发送</button>
        </div>
      </div>
      <footer id="footer">本地服务运行中…</footer>
    </div>
  </div>

  <div class="modal-mask" id="answerModal">
    <div class="modal modal-answer">
      <h3>补全答案</h3>
      <p class="modal-hint">支持 Markdown（列表 / **加粗** / `代码`）。可用「整理排版」把顿号长列表拆成条目，再点「预览」查看效果。</p>
      <div class="qbox" id="modalQuestion"></div>
      <div class="meta-grid">
        <label>标签（逗号分隔）
          <input id="modalTags" placeholder="feishu, frontend" />
        </label>
        <label>类型
          <select id="modalKind">
            <option value="qa">qa 问答</option>
            <option value="command">command 命令</option>
            <option value="path">path 路径</option>
            <option value="env">env 环境</option>
            <option value="pitfall">pitfall 踩坑</option>
            <option value="decision">decision 决策</option>
          </select>
        </label>
        <label class="span2">结构化补充（可选，随类型填写）
          <input id="modalFact" placeholder="如：pnpm build 或 ~/path 或 KEY=value" />
        </label>
      </div>
      <div class="answer-toolbar">
        <div class="answer-tabs" id="answerTabs">
          <button type="button" class="active" data-pane="edit">编辑</button>
          <button type="button" data-pane="preview">预览</button>
        </div>
        <button type="button" class="ghost" id="btnTidyAnswer" title="把 1)…（a、b、c）整理成 Markdown 列表">整理排版</button>
      </div>
      <div class="answer-panes">
        <textarea id="modalAnswer" placeholder="在此输入答案（答）&#10;&#10;示例：&#10;1. **包名**：一句话说明&#10;   - 模块 A&#10;   - 模块 B"></textarea>
        <div id="modalPreview" class="answer-preview md hidden"></div>
      </div>
      <div class="modal-actions">
        <button type="button" id="btnModalCancel">取消</button>
        <button type="button" class="primary" id="btnModalOk">确认记住</button>
      </div>
    </div>
  </div>

  <div class="modal-mask" id="confirmClearModal">
    <div class="modal">
      <h3 id="confirmClearTitle">确认清空长时记忆</h3>
      <p class="warn" id="confirmClearMsg">此操作不可撤销。</p>
      <label class="check">
        <input type="checkbox" id="confirmBackupFirst" checked />
        <span>清空前先备份长时记忆（推荐）</span>
      </label>
      <div class="modal-actions">
        <button type="button" id="btnConfirmClearCancel">取消</button>
        <button type="button" class="danger" id="btnConfirmClearOk">确认清空</button>
      </div>
    </div>
  </div>

<script>
const chat = document.getElementById('chat');
const input = document.getElementById('input');
const footer = document.getElementById('footer');
const btnSend = document.getElementById('btnSend');
const slashMenu = document.getElementById('slashMenu');
const slashItems = document.getElementById('slashItems');
const modeBar = document.getElementById('modeBar');
const qaList = document.getElementById('qaList');
const answerModal = document.getElementById('answerModal');
const modalQuestion = document.getElementById('modalQuestion');
const modalAnswer = document.getElementById('modalAnswer');

const SOURCE = {
  working: '工作记忆', long_term: '长时记忆', procedural: '程序性记忆',
  llm: '大模型', command: '指令', sensory_reject: '感觉记忆'
};

const COMMANDS = [
  { id: 'memory', name: '记忆', desc: '把输入当作「问」，左侧列出；点选后弹窗填写「答」', icon: '记' },
  { id: 'help', name: '帮助', desc: '显示可用指令说明', icon: '帮', run: '帮助' },
  { id: 'status', name: '记忆状态', desc: '查看各层统计', icon: '态', run: '查看记忆状态' },
  { id: 'working', name: '短时记忆', desc: '查看工作记忆窗口', icon: '短', run: '查看短时记忆' },
  { id: 'long', name: '长时记忆', desc: '查看长时记忆清单', icon: '长', run: '查看长时记忆' },
  { id: 'clear_w', name: '清空工作记忆', desc: '清空短时滑动窗口', icon: '清', run: '清空工作记忆' },
  { id: 'backup_l', name: '备份长时记忆', desc: '导出陈述性问答到 backups/', icon: '备', run: '备份长时记忆' },
  { id: 'clear_l', name: '清空长时记忆', desc: '清空持久化问答（需确认）', icon: '删', confirmClear: 'long' },
  { id: 'extract', name: '提炼候选', desc: '从粘贴的终端/日志提炼候选记忆', icon: '炼', extract: true },
];

let activeTab = 'pending';
let pendingQuestions = loadPending();
let savedMemories = [];
let activeTagFilter = '';
let mode = null; // 'memory' | null
let slashIndex = 0;
let editingQuestion = '';
let editingRecordId = '';

function loadPending() {
  try { return JSON.parse(localStorage.getItem('ms_pending_q') || '[]'); }
  catch (e) { return []; }
}
function savePending() {
  localStorage.setItem('ms_pending_q', JSON.stringify(pendingQuestions));
}

function append(text, cls, opts) {
  const d = document.createElement('div');
  d.className = 'msg ' + cls;
  if (opts && opts.markdown) {
    d.classList.add('md');
    if (opts.label) {
      d.innerHTML = '<div class="bot-label">' + escapeHtml(opts.label) + '</div>' + renderMarkdown(text);
    } else {
      d.innerHTML = renderMarkdown(text);
    }
  } else {
    d.textContent = text;
  }
  chat.appendChild(d);
  chat.scrollTop = chat.scrollHeight;
}

async function api(path, body) {
  const res = await fetch(path, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: body ? JSON.stringify(body) : '{}'
  });
  return res.json();
}

function setMode(next) {
  mode = next;
  modeBar.classList.toggle('show', mode === 'memory' || mode === 'extract');
  const label = modeBar.querySelector('span');
  if (mode === 'memory') {
    if (label) label.textContent = '当前指令：记忆 — 请输入「问」';
    input.placeholder = '记忆模式：输入问题（问），Enter 加入左侧清单';
  } else if (mode === 'extract') {
    if (label) label.textContent = '当前指令：提炼 — 粘贴终端/日志';
    input.placeholder = '提炼模式：粘贴终端输出或日志，Enter 提炼候选';
  } else {
    if (label) label.textContent = '当前指令：记忆 — 请输入「问」';
    input.placeholder = '输入问题，或输入 / 选择指令。Enter 发送，Shift+Enter 换行';
  }
}

function hideSlash() {
  slashMenu.classList.remove('show');
}

function filteredCommands() {
  const raw = input.value;
  if (!raw.startsWith('/')) return [];
  const q = raw.slice(1).trim().toLowerCase();
  return COMMANDS.filter(c =>
    !q || c.name.toLowerCase().includes(q) || c.id.includes(q) || c.desc.includes(q)
  );
}

function renderSlash() {
  const list = filteredCommands();
  if (!input.value.startsWith('/') || !list.length) {
    hideSlash();
    return;
  }
  if (slashIndex >= list.length) slashIndex = 0;
  slashItems.innerHTML = '';
  list.forEach((c, i) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'slash-item' + (i === slashIndex ? ' active' : '');
    btn.innerHTML =
      '<div class="slash-ico">' + c.icon + '</div>' +
      '<div><div class="slash-name">/' + c.name + '</div>' +
      '<div class="slash-desc">' + c.desc + '</div></div>';
    btn.onclick = () => selectCommand(c);
    slashItems.appendChild(btn);
  });
  slashMenu.classList.add('show');
}

async function selectCommand(cmd) {
  hideSlash();
  input.value = '';
  if (cmd.id === 'memory') {
    setMode('memory');
    append('已进入「记忆」指令：请输入问题（问）。提交后会出现在左侧，点选再填写答案。', 'sys');
    input.focus();
    return;
  }
  if (cmd.extract) {
    setMode('extract');
    append('已进入「提炼候选」：粘贴终端/日志文本到下方发送，将返回候选记忆供确认写入。', 'sys');
    input.focus();
    return;
  }
  if (cmd.confirmClear) {
    openConfirmClear(cmd.confirmClear);
    return;
  }
  if (cmd.run) {
    await sendText(cmd.run);
  }
}

async function runExtract(text) {
  const data = await api('/api/extract', { text, max_n: 3 });
  if (data.error) {
    append('错误：' + data.error, 'meta');
    return;
  }
  const cands = data.candidates || [];
  if (!cands.length) {
    append('未提炼出候选记忆，可换一段包含命令/路径/报错的文本再试。', 'sys');
    return;
  }
  append('提炼到 ' + cands.length + ' 条候选（点击左侧填写或直接点「记住」）：', 'sys');
  cands.forEach((c, i) => {
    const label = '[' + (i + 1) + '] ' + (c.kind || 'qa') + ' · ' + (c.question || '');
    append(label + '\n' + (c.answer || ''), 'bot', { markdown: false, label: '候选' });
    pendingQuestions.push(c.question || ('候选' + (i + 1)));
    // 用 session 暂存候选详情，点开弹窗时回填
    try {
      const map = JSON.parse(sessionStorage.getItem('ms_extract_map') || '{}');
      map[c.question] = c;
      sessionStorage.setItem('ms_extract_map', JSON.stringify(map));
    } catch (e) {}
  });
  savePending();
  activeTab = 'pending';
  document.querySelectorAll('.side-tabs button').forEach(b => {
    b.classList.toggle('active', b.dataset.tab === 'pending');
  });
  renderQaList();
  if (data.suggested_tags && data.suggested_tags.length) {
    append('建议标签：' + data.suggested_tags.map((t) => '#' + t).join(' '), 'meta');
  }
}

function isClearLongCommand(text) {
  return ['清空长时记忆', '清空长期记忆', '清空持久记忆'].includes(text);
}
function isClearAllCommand(text) {
  return ['清空全部记忆', '清空所有记忆'].includes(text);
}

function openConfirmClear(kind) {
  const modal = document.getElementById('confirmClearModal');
  const title = document.getElementById('confirmClearTitle');
  const msg = document.getElementById('confirmClearMsg');
  const n = savedMemories.length;
  modal.dataset.kind = kind || 'long';
  if (kind === 'all') {
    title.textContent = '确认清空全部记忆';
    msg.textContent = '将清空工作记忆、感觉记忆，以及全部 ' + n + ' 条长时陈述性问答。此操作不可撤销。';
  } else {
    title.textContent = '确认清空长时记忆';
    msg.textContent = '将清空全部 ' + n + ' 条长时陈述性问答（程序性模板保留）。此操作不可撤销。';
  }
  document.getElementById('confirmBackupFirst').checked = true;
  modal.classList.add('show');
}

function closeConfirmClear() {
  document.getElementById('confirmClearModal').classList.remove('show');
}

async function executeConfirmClear() {
  const modal = document.getElementById('confirmClearModal');
  const kind = modal.dataset.kind || 'long';
  const backupFirst = document.getElementById('confirmBackupFirst').checked;
  closeConfirmClear();
  if (kind === 'all') {
    const cmd = backupFirst ? '确认清空全部记忆并备份' : '确认清空全部记忆';
    await sendText(cmd);
    return;
  }
  try {
    const data = await api('/api/clear_long_term', {
      confirm: true,
      backup_first: backupFirst,
    });
    if (data.error) append('错误：' + data.error, 'meta');
    else append(data.message || '长时记忆已清空', 'sys');
    if (data.status_line) footer.textContent = data.status_line;
    pendingQuestions = [];
    savePending();
    await refreshSaved();
  } catch (e) {
    append('请求失败：' + e, 'meta');
  }
}

function allSavedTags() {
  const set = new Set();
  savedMemories.forEach((rec) => (rec.tags || []).forEach((t) => set.add(t)));
  return Array.from(set).sort();
}

function renderTagFilter() {
  const bar = document.getElementById('tagFilter');
  if (!bar) return;
  if (activeTab !== 'saved') {
    bar.hidden = true;
    bar.innerHTML = '';
    return;
  }
  const tags = allSavedTags();
  if (!tags.length) {
    bar.hidden = true;
    bar.innerHTML = '';
    return;
  }
  bar.hidden = false;
  const chips = [{ tag: '', label: '全部' }].concat(tags.map((t) => ({ tag: t, label: '#' + t })));
  bar.innerHTML = chips.map((c) =>
    '<button type="button" class="tag-chip' + ((activeTagFilter === c.tag) ? ' active' : '') +
    '" data-tag="' + escapeHtml(c.tag) + '">' + escapeHtml(c.label) + '</button>'
  ).join('');
  bar.querySelectorAll('.tag-chip').forEach((btn) => {
    btn.onclick = () => {
      activeTagFilter = btn.dataset.tag || '';
      renderQaList();
    };
  });
}

function filteredSaved() {
  if (!activeTagFilter) return savedMemories;
  return savedMemories.filter((rec) => (rec.tags || []).includes(activeTagFilter));
}

function renderQaList() {
  qaList.innerHTML = '';
  renderTagFilter();
  if (activeTab === 'pending') {
    if (!pendingQuestions.length) {
      qaList.innerHTML = '<div class="qa-a" style="padding:8px">暂无待补全的问题。输入 / 选择「记忆」添加。</div>';
      return;
    }
    pendingQuestions.forEach((q, idx) => {
      const el = document.createElement('div');
      el.className = 'qa-item pending';
      el.innerHTML =
        '<div class="qa-top">' +
          '<div class="qa-badge pending-badge">待补全答</div>' +
          '<button type="button" class="qa-del" data-pending-idx="' + idx + '">删除</button>' +
        '</div>' +
        '<p class="qa-q">' + escapeHtml(q) + '</p>' +
        '<p class="qa-a">点击填写答案</p>';
      el.onclick = () => openAnswerModal(q, '', idx);
      el.querySelector('.qa-del').onclick = (ev) => {
        ev.stopPropagation();
        deletePending(idx);
      };
      qaList.appendChild(el);
    });
    return;
  }
  const list = filteredSaved();
  if (!savedMemories.length) {
    qaList.innerHTML = '<div class="qa-a" style="padding:8px">暂无已记住的问答。</div>';
    return;
  }
  if (!list.length) {
    qaList.innerHTML = '<div class="qa-a" style="padding:8px">当前标签下无记忆。</div>';
    return;
  }
  list.forEach((rec) => {
    const el = document.createElement('div');
    el.className = 'qa-item';
    el.innerHTML =
      '<div class="qa-top">' +
        '<div class="qa-badge">' + escapeHtml(rec.scene || 'general') + '</div>' +
        (rec.kind && rec.kind !== 'qa'
          ? '<div class="qa-badge kind">' + escapeHtml(rec.kind) + '</div>'
          : '') +
        ((rec.tags && rec.tags.length)
          ? '<div class="qa-badge" style="opacity:.85">#' + escapeHtml(rec.tags.join(' #')) + '</div>'
          : '') +
        '<button type="button" class="qa-del" data-id="' + escapeHtml(rec.id || '') + '">删除</button>' +
      '</div>' +
      '<p class="qa-q">' + escapeHtml(rec.question) + '</p>' +
      '<div class="qa-a md">' + renderMarkdown(rec.answer || '') + '</div>';
    el.onclick = () => openAnswerModal(rec.question, rec.answer, -1, rec);
    el.querySelector('.qa-del').onclick = (ev) => {
      ev.stopPropagation();
      deleteSaved(rec);
    };
    qaList.appendChild(el);
  });
}

function deletePending(idx) {
  if (!confirm('删除这条待补全的问题？')) return;
  pendingQuestions.splice(idx, 1);
  savePending();
  renderQaList();
  append('已删除待补全问题。', 'sys');
}

async function deleteSaved(rec) {
  const title = rec.question || rec.id || '';
  if (!confirm('确定删除已记住的「' + title + '」？此操作不可恢复。')) return;
  const data = await api('/api/delete_memory', { id: rec.id, question: rec.question });
  if (data.error) {
    append('错误：' + data.error, 'meta');
    return;
  }
  append(data.message || '已删除', 'sys');
  if (data.status_line) footer.textContent = data.status_line;
  await refreshSaved();
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function inlineMarkdown(escaped) {
  return escaped
    .replace(/`([^`\n]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/__([^_]+)__/g, '<strong>$1</strong>');
}

function renderMarkdown(src) {
  const text = String(src || '').replace(/\r\n/g, '\n');
  const parts = [];
  const fence = /```([\s\S]*?)```/g;
  let last = 0;
  let m;
  while ((m = fence.exec(text))) {
    if (m.index > last) parts.push({ type: 'text', value: text.slice(last, m.index) });
    parts.push({ type: 'code', value: m[1].replace(/^\n/, '').replace(/\n$/, '') });
    last = m.index + m[0].length;
  }
  if (last < text.length) parts.push({ type: 'text', value: text.slice(last) });

  function renderTextBlock(block) {
    const lines = block.split('\n');
    let html = '';
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (/^\s*[-*]\s+/.test(line)) {
        html += '<ul>';
        while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) {
          html += '<li>' + inlineMarkdown(escapeHtml(lines[i].replace(/^\s*[-*]\s+/, ''))) + '</li>';
          i++;
        }
        html += '</ul>';
        continue;
      }
      if (/^\s*\d+[.)]\s+/.test(line)) {
        html += '<ol>';
        while (i < lines.length && /^\s*\d+[.)]\s+/.test(lines[i])) {
          html += '<li>' + inlineMarkdown(escapeHtml(lines[i].replace(/^\s*\d+[.)]\s+/, ''))) + '</li>';
          i++;
        }
        html += '</ol>';
        continue;
      }
      if (/^\s*#{1,3}\s+/.test(line)) {
        const hm = line.match(/^\s*(#{1,3})\s+(.*)$/);
        const level = hm[1].length;
        html += '<h' + level + '>' + inlineMarkdown(escapeHtml(hm[2])) + '</h' + level + '>';
        i++;
        continue;
      }
      if (!line.trim()) { i++; continue; }
      let para = line;
      i++;
      while (i < lines.length && lines[i].trim() && !/^\s*[-*]\s+/.test(lines[i]) && !/^\s*\d+[.)]\s+/.test(lines[i]) && !/^\s*#{1,3}\s+/.test(lines[i])) {
        para += '\n' + lines[i];
        i++;
      }
      html += '<p>' + inlineMarkdown(escapeHtml(para)).replace(/\n/g, '<br>') + '</p>';
    }
    return html;
  }

  let out = '';
  for (const p of parts) {
    if (p.type === 'code') {
      out += '<pre><code>' + escapeHtml(p.value) + '</code></pre>';
    } else {
      out += renderTextBlock(p.value);
    }
  }
  return out || '<p></p>';
}

/** 把「1) 标题：说明（a、b、c）」整理成更易读的 Markdown */
function tidyAnswerLayout(raw) {
  let text = String(raw || '').replace(/\r\n/g, '\n').trim();
  if (!text) return text;
  // 粘在一起的编号项拆到新行
  text = text.replace(/([^\n])\s*(?=\d+[)）．.]\s*)/g, '$1\n');
  const chunks = text.split(/\n+/).filter(Boolean);
  const out = [];
  for (let chunk of chunks) {
    chunk = chunk.trim();
    const num = chunk.match(/^(\d+)[)）．.]\s*(.*)$/);
    let body = num ? num[2] : chunk;
    let head = num ? (num[1] + '. ') : '';
    // 去掉已有加粗，避免 ****标题****
    body = body.replace(/\*{2,}/g, '');
    // 包名/标题加粗：xxx： 或 xxx:
    body = body.replace(/^([A-Za-z0-9_./\-]+|[^\s：:]{1,40})\s*[:：]\s*/, (_, title) => {
      return '**' + title.trim() + '**：';
    });
    // 全角/半角括号内顿号列表 → 子 bullet
    body = body.replace(/[（(]([^）)]+)[）)]/g, (all, inner) => {
      const items = inner.split(/[、,，;；]/).map((s) => s.trim()).filter(Boolean);
      if (items.length < 2) return all;
      return '\n' + items.map((it) => '   - `' + it.replace(/^`|`$/g, '') + '`').join('\n');
    });
    out.push(head + body);
  }
  return out.join('\n\n').replace(/\n{3,}/g, '\n\n').trim();
}

function setAnswerPane(pane) {
  const editBtn = document.querySelector('#answerTabs [data-pane="edit"]');
  const prevBtn = document.querySelector('#answerTabs [data-pane="preview"]');
  const preview = document.getElementById('modalPreview');
  if (!editBtn || !prevBtn || !preview || !modalAnswer) return;
  const isPreview = pane === 'preview';
  editBtn.classList.toggle('active', !isPreview);
  prevBtn.classList.toggle('active', isPreview);
  modalAnswer.classList.toggle('hidden', isPreview);
  preview.classList.toggle('hidden', !isPreview);
  if (isPreview) preview.innerHTML = renderMarkdown(modalAnswer.value || '_（空）_');
}

async function refreshSaved() {
  const data = await api('/api/list_memory', { layer: 'long_term' });
  savedMemories = (data.data && data.data.declarative) || [];
  if (data.status_line) footer.textContent = data.status_line;
  renderQaList();
}

function openAnswerModal(question, answer, pendingIndex, rec) {
  let seed = rec || null;
  if (!seed) {
    try {
      const map = JSON.parse(sessionStorage.getItem('ms_extract_map') || '{}');
      seed = map[question] || null;
    } catch (e) { seed = null; }
  }
  editingQuestion = question;
  editingRecordId = (seed && seed.id) || '';
  modalQuestion.textContent = question;
  modalAnswer.value = answer || (seed && seed.answer) || '';
  const tagsEl = document.getElementById('modalTags');
  const kindEl = document.getElementById('modalKind');
  const factEl = document.getElementById('modalFact');
  if (tagsEl) tagsEl.value = ((seed && seed.tags) || []).join(', ');
  if (kindEl) kindEl.value = (seed && seed.kind) || 'qa';
  if (factEl) {
    const facts = (seed && seed.facts) || {};
    const kind = (seed && seed.kind) || 'qa';
    factEl.value = facts[kind] || facts.command || facts.path || facts.env || facts.pitfall || facts.decision || '';
  }
  answerModal.dataset.pendingIndex = String(pendingIndex);
  answerModal.classList.add('show');
  setAnswerPane('edit');
  modalAnswer.focus();
}

function closeAnswerModal() {
  answerModal.classList.remove('show');
  editingQuestion = '';
  editingRecordId = '';
  setAnswerPane('edit');
}

function parseTagsInput(raw) {
  return String(raw || '')
    .split(/[,，\s]+/)
    .map((s) => s.replace(/^#/, '').trim())
    .filter(Boolean);
}

async function confirmAnswer() {
  const answer = modalAnswer.value.trim();
  const question = editingQuestion.trim();
  if (!question || !answer) {
    alert('问题和答案都不能为空');
    return;
  }
  const kind = (document.getElementById('modalKind') || {}).value || 'qa';
  const tags = parseTagsInput((document.getElementById('modalTags') || {}).value);
  const factVal = ((document.getElementById('modalFact') || {}).value || '').trim();
  const facts = {};
  if (factVal && kind && kind !== 'qa') facts[kind] = factVal;
  const data = await api('/api/remember', {
    question, answer, scene: 'dev', tags, kind, facts,
  });
  if (data.error) {
    append('错误：' + data.error, 'meta');
    return;
  }
  const pIdx = parseInt(answerModal.dataset.pendingIndex || '-1', 10);
  if (pIdx >= 0) {
    pendingQuestions.splice(pIdx, 1);
    savePending();
  } else {
    // 若问题原先在待补全列表里，一并移除
    pendingQuestions = pendingQuestions.filter(q => q !== question);
    savePending();
  }
  closeAnswerModal();
  append('已记住：' + question, 'sys');
  append(answer, 'bot', { markdown: true, label: '答' });
  activeTab = 'saved';
  document.querySelectorAll('.side-tabs button').forEach(b => {
    b.classList.toggle('active', b.dataset.tab === 'saved');
  });
  await refreshSaved();
  if (data.status_line) footer.textContent = data.status_line;
}

function createThinkCard() {
  const card = document.createElement('div');
  card.className = 'think-card';
  card.innerHTML =
    '<div class="think-head">' +
      '<span class="think-spinner"></span>' +
      '<span class="think-title">思考中</span>' +
      '<span class="elapsed">0s</span>' +
    '</div>' +
    '<ul class="think-steps"></ul>';
  chat.appendChild(card);
  chat.scrollTop = chat.scrollHeight;
  const t0 = Date.now();
  const timer = setInterval(() => {
    const el = card.querySelector('.elapsed');
    if (el) el.textContent = Math.floor((Date.now() - t0) / 1000) + 's';
  }, 250);
  const steps = card.querySelector('.think-steps');
  return {
    el: card,
    addProgress(msg) {
      const prev = steps.querySelector('li.active');
      if (prev) {
        prev.classList.remove('active');
        prev.classList.add('done');
        const m = prev.querySelector('.mark');
        if (m) m.textContent = '✓';
      }
      const li = document.createElement('li');
      li.className = 'active';
      const short = String(msg || '').replace(/…/g, '').trim();
      li.innerHTML = '<span class="mark">●</span><span class="text"></span>';
      li.querySelector('.text').textContent = short || msg;
      steps.appendChild(li);
      chat.scrollTop = chat.scrollHeight;
    },
    finish(ok) {
      clearInterval(timer);
      const prev = steps.querySelector('li.active');
      if (prev) {
        prev.classList.remove('active');
        prev.classList.add('done');
        const m = prev.querySelector('.mark');
        if (m) m.textContent = ok ? '✓' : '!';
      }
      card.classList.add(ok ? 'done' : 'error');
      const title = card.querySelector('.think-title');
      if (title) title.textContent = ok ? '思考完成' : '思考中断';
      const el = card.querySelector('.elapsed');
      if (el) el.textContent = Math.floor((Date.now() - t0) / 1000) + 's';
    }
  };
}

async function sendText(text) {
  append('你：' + text, 'user');
  btnSend.disabled = true;
  const think = createThinkCard();
  try {
    const res = await fetch('/api/chat_stream', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ text })
    });
    if (!res.ok || !res.body) {
      think.finish(false);
      append('错误：流式接口不可用（HTTP ' + res.status + '）', 'meta');
      return;
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder('utf-8');
    let buf = '';
    let finalData = null;
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf('\n')) >= 0) {
        const line = buf.slice(0, idx).trim();
        buf = buf.slice(idx + 1);
        if (!line) continue;
        let ev;
        try { ev = JSON.parse(line); } catch (e) { continue; }
        if (ev.type === 'progress') think.addProgress(ev.message || '');
        else if (ev.type === 'done' || ev.type === 'error') finalData = ev;
      }
    }
    if (buf.trim()) {
      try {
        const ev = JSON.parse(buf.trim());
        if (ev.type === 'progress') think.addProgress(ev.message || '');
        else if (ev.type === 'done' || ev.type === 'error') finalData = ev;
      } catch (e) {}
    }
    if (!finalData) {
      think.finish(false);
      append('错误：未收到完整结果', 'meta');
      return;
    }
    if (finalData.type === 'error' || finalData.error) {
      think.finish(false);
      append('错误：' + (finalData.error || '未知错误'), 'meta');
    } else {
      think.finish(true);
      append(finalData.answer || '', 'bot', { markdown: true, label: '沙箱' });
      append('← 来源：' + (SOURCE[finalData.source] || finalData.source) + ' (' + finalData.source + ')', 'meta');
    }
    if (finalData.status_line) footer.textContent = finalData.status_line;
    if (text.startsWith('记住') || text.includes('清空长时') || text.includes('清空全部')) {
      await refreshSaved();
      renderQaList();
    }
  } catch (e) {
    think.finish(false);
    append('请求失败：' + e, 'meta');
  } finally {
    btnSend.disabled = false;
    input.focus();
  }
}

async function send() {
  const text = input.value.trim();
  if (!text) return;

  // slash 选择中回车：执行当前高亮指令
  if (text.startsWith('/') && slashMenu.classList.contains('show')) {
    const list = filteredCommands();
    if (list.length) {
      await selectCommand(list[slashIndex] || list[0]);
      return;
    }
  }

  // 危险指令：先弹确认，不直接发送
  if (isClearLongCommand(text) || isClearAllCommand(text)) {
    input.value = '';
    hideSlash();
    openConfirmClear(isClearAllCommand(text) ? 'all' : 'long');
    return;
  }

  input.value = '';
  hideSlash();

  if (mode === 'memory') {
    if (pendingQuestions.includes(text)) {
      append('该问题已在待补全列表中。', 'sys');
    } else {
      pendingQuestions.unshift(text);
      savePending();
      activeTab = 'pending';
      document.querySelectorAll('.side-tabs button').forEach(b => {
        b.classList.toggle('active', b.dataset.tab === 'pending');
      });
      renderQaList();
      append('已加入左侧「问」：' + text + '\n请在左侧点选该问题，弹窗中填写「答」。', 'sys');
    }
    setMode(null);
    return;
  }

  if (mode === 'extract') {
    append(text.slice(0, 400) + (text.length > 400 ? '…' : ''), 'user');
    setMode(null);
    await runExtract(text);
    return;
  }

  await sendText(text);
}

btnSend.onclick = send;
input.addEventListener('input', () => {
  if (input.value.startsWith('/')) {
    slashIndex = 0;
    renderSlash();
  } else {
    hideSlash();
  }
});
input.addEventListener('keydown', (e) => {
  if (slashMenu.classList.contains('show')) {
    const list = filteredCommands();
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      slashIndex = (slashIndex + 1) % Math.max(list.length, 1);
      renderSlash();
      return;
    }
    if (e.key === 'ArrowUp') {
      e.preventDefault();
      slashIndex = (slashIndex - 1 + list.length) % Math.max(list.length, 1);
      renderSlash();
      return;
    }
    if (e.key === 'Escape') {
      hideSlash();
      return;
    }
  }
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    send();
  }
});

document.getElementById('btnCancelMode').onclick = () => setMode(null);
document.getElementById('btnModalCancel').onclick = closeAnswerModal;
document.getElementById('btnModalOk').onclick = confirmAnswer;
answerModal.addEventListener('click', (e) => {
  if (e.target === answerModal) closeAnswerModal();
});
document.getElementById('answerTabs').onclick = (e) => {
  const btn = e.target.closest('button[data-pane]');
  if (!btn) return;
  setAnswerPane(btn.dataset.pane);
};
document.getElementById('btnTidyAnswer').onclick = () => {
  const before = modalAnswer.value;
  const after = tidyAnswerLayout(before);
  if (after === before.trim()) {
    alert('未识别到可整理的编号/顿号列表。可手动用 Markdown：1. **标题** / - 条目');
    return;
  }
  modalAnswer.value = after;
  setAnswerPane('preview');
};
document.getElementById('btnConfirmClearCancel').onclick = closeConfirmClear;
document.getElementById('btnConfirmClearOk').onclick = executeConfirmClear;
document.getElementById('confirmClearModal').addEventListener('click', (e) => {
  if (e.target === document.getElementById('confirmClearModal')) closeConfirmClear();
});

document.querySelectorAll('.side-tabs button').forEach(btn => {
  btn.onclick = () => {
    activeTab = btn.dataset.tab;
    document.querySelectorAll('.side-tabs button').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    renderQaList();
  };
});

async function showMemory(layer) {
  const data = await api('/api/list_memory', { layer });
  if (data.error) append('错误：' + data.error, 'meta');
  else append(data.text || '', 'meta');
  if (data.status_line) footer.textContent = data.status_line;
}
document.getElementById('btnWorking').onclick = () => showMemory('working');
document.getElementById('btnLong').onclick = () => showMemory('long_term');
document.getElementById('btnAllMem').onclick = () => showMemory('all');
document.getElementById('btnStatus').onclick = async () => {
  const data = await api('/api/status');
  append(JSON.stringify(data.status, null, 2), 'meta');
  footer.textContent = data.status_line || footer.textContent;
};
document.getElementById('btnClear').onclick = async () => {
  const data = await api('/api/clear_working');
  append(data.message || '工作记忆已清空', 'sys');
  footer.textContent = data.status_line || footer.textContent;
};
document.getElementById('btnBackupLong').onclick = async () => {
  try {
    const data = await api('/api/backup_long_term');
    if (data.error) append('错误：' + data.error, 'meta');
    else append(data.message || '已备份', 'sys');
    if (data.status_line) footer.textContent = data.status_line;
  } catch (e) {
    append('请求失败：' + e, 'meta');
  }
};
document.getElementById('btnExportPack').onclick = async () => {
  try {
    const name = prompt('知识包名称', 'memory-pack') || 'memory-pack';
    const data = await api('/api/export_pack', {
      name,
      filter_tags: activeTagFilter ? [activeTagFilter] : undefined,
    });
    if (data.error) append('错误：' + data.error, 'meta');
    else append(data.message || '已导出知识包', 'sys');
    if (data.status_line) footer.textContent = data.status_line;
  } catch (e) {
    append('请求失败：' + e, 'meta');
  }
};
document.getElementById('btnGitCheck').onclick = async () => {
  try {
    const data = await api('/api/git_check', {});
    if (data.error) {
      append('错误：' + data.error, 'meta');
      return;
    }
    const stale = data.stale || [];
    append(data.hint || '已检查 Git 变更与记忆关联', 'sys');
    if (!stale.length) return;
    stale.forEach((h) => {
      append(
        '可能过时：' + (h.question || '') + '\n相关文件：' + ((h.matched_paths || []).join(', ') || '-'),
        'meta'
      );
    });
  } catch (e) {
    append('请求失败：' + e, 'meta');
  }
};
document.getElementById('btnArchive').onclick = async () => {
  if (!confirm('确认归档久未命中的长时记忆？（默认按配置 aging_days）')) return;
  try {
    const data = await api('/api/archive', { confirm: true });
    if (data.error) append('错误：' + data.error, 'meta');
    else append(data.message || '已归档', 'sys');
    if (data.status_line) footer.textContent = data.status_line;
    await refreshSaved();
  } catch (e) {
    append('请求失败：' + e, 'meta');
  }
};
document.getElementById('btnClearLong').onclick = () => openConfirmClear('long');
document.getElementById('btnSeed').onclick = async () => {
  const data = await api('/api/seed');
  append(data.message || '已写入种子记忆', 'sys');
  footer.textContent = data.status_line || footer.textContent;
  await refreshSaved();
};
document.getElementById('btnData').onclick = async () => {
  const data = await api('/api/open_data');
  append(data.message || data.path, 'sys');
};
document.getElementById('btnQuit').onclick = async () => {
  if (!confirm('确定退出记忆沙箱？')) return;
  append('正在退出…', 'sys');
  try { await api('/api/shutdown'); } catch (e) {}
  footer.textContent = '应用已退出，可关闭此页面';
  document.querySelectorAll('button, textarea').forEach((el) => { el.disabled = true; });
};

const agentModeSel = document.getElementById('agentMode');
async function refreshAgentMode() {
  try {
    const data = await api('/api/status');
    const m = (data.status && data.status.llm && data.status.llm.agent_mode) || 'ask';
    if (agentModeSel) agentModeSel.value = m;
    if (data.status_line) footer.textContent = data.status_line;
  } catch (e) {}
}
if (agentModeSel) {
  agentModeSel.onchange = async () => {
    const mode = agentModeSel.value;
    if (mode === 'agent' && !confirm('切换为 Agent 可写模式？本地 LLM 可能修改工作区文件并执行命令。')) {
      await refreshAgentMode();
      return;
    }
    try {
      const data = await api('/api/agent_mode', { mode, persist: true });
      append(data.message || ('已切换 Agent 模式：' + mode), 'sys');
      if (data.status_line) footer.textContent = data.status_line;
    } catch (e) {
      append('切换失败：' + (e.message || e), 'sys');
      await refreshAgentMode();
    }
  };
}

append('优先检索本地三级记忆。\\n提问后会显示「思考中」过程（本地检索 → LLM）。\\n新方式：输入 / 选择「记忆」→ 输入问 → 左侧点选 → 弹窗填答 → 确认。\\n原有指令仍可用：记住：问 => 答 | 备份长时记忆 | 清空长时记忆（需确认）| 帮助\\n工具栏可切换 Agent 模式：Ask 只读 / Plan / Agent 可写。', 'sys');
refreshSaved();
renderQaList();
refreshAgentMode();
(async function ensureStreamUi() {
  try {
    const r = await fetch('/api/health', { cache: 'no-store' });
    const h = await r.json();
    const feats = h.features || [];
    if (!feats.includes('chat_stream')) {
      append('当前后台过旧，没有「思考过程」流。请点「退出应用」，再运行：python3 app_web.py', 'meta');
    } else if (h.build) {
      footer.title = 'build ' + h.build;
    }
  } catch (e) {}
})();
input.focus();
</script>
</body>
</html>
"""


class AppState:
    def __init__(self):
        cfg_path = str(default_config_path())
        cfg = load_config(cfg_path)
        cfg.long_term.persist_dir = str(default_persist_dir())
        self.sandbox = MemorySandbox(config=cfg, config_path=cfg_path)

    def status_line(self) -> str:
        st = self.sandbox.status()
        am = (st.get("llm") or {}).get("agent_mode") or "ask"
        return (
            f"工作记忆 {st['working']['size']}/{st['working']['max_size']} · "
            f"长时记忆 {st['long_term']['declarative_count']} 条 · "
            f"场景 {st['working']['scene']} · "
            f"Agent {am} · "
            f"数据 {app_support_dir()}"
        )


STATE: AppState
SERVER: Optional[ThreadingHTTPServer] = None


def _json_response(handler: BaseHTTPRequestHandler, code: int, payload: dict):
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(raw)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(raw)


def _html_response(handler: BaseHTTPRequestHandler, html: str):
    # 注入 build，避免浏览器/旧页缓存导致「没有分析过程」
    injected = html.replace(
        "</title>",
        f"</title>\n<meta name=\"ms-build\" content=\"{UI_BUILD}\" />",
        1,
    )
    raw = injected.encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
    handler.send_header("Pragma", "no-cache")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            _html_response(self, HTML_PAGE)
            return
        if path == "/api/health":
            _json_response(
                self,
                200,
                {
                    "ok": True,
                    "app": "memory-sandbox",
                    "build": UI_BUILD,
                    "features": list(UI_FEATURES),
                },
            )
            return
        self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(body.decode("utf-8") or "{}")
        except Exception:
            data = {}

        try:
            if path == "/api/chat":
                text = (data.get("text") or "").strip()
                result = STATE.sandbox.chat(text)
                _json_response(
                    self,
                    200,
                    {
                        "answer": result.answer,
                        "source": result.source,
                        "status_line": STATE.status_line(),
                    },
                )
                return
            if path == "/api/chat_stream":
                text = (data.get("text") or "").strip()
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
                self.send_header("Cache-Control", "no-cache, no-store")
                self.send_header("X-Accel-Buffering", "no")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()

                def _emit(obj: dict) -> None:
                    # HTTP chunked，避免整段缓冲导致前端长时间无进度
                    payload = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
                    self.wfile.write(f"{len(payload):X}\r\n".encode("ascii"))
                    self.wfile.write(payload)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()

                def _end_chunks() -> None:
                    try:
                        self.wfile.write(b"0\r\n\r\n")
                        self.wfile.flush()
                    except Exception:
                        pass

                ev_q: "queue.Queue[Optional[dict]]" = queue.Queue()
                holder: dict = {}

                def on_progress(msg: str) -> None:
                    ev_q.put({"type": "progress", "message": msg})

                def worker() -> None:
                    try:
                        result = STATE.sandbox.chat(text, on_progress=on_progress)
                        holder["result"] = result
                    except Exception as e:
                        holder["error"] = str(e)
                        holder["trace"] = traceback.format_exc()
                    finally:
                        ev_q.put(None)

                try:
                    _emit({"type": "progress", "message": "开始处理…"})
                except Exception:
                    _end_chunks()
                    return
                threading.Thread(target=worker, daemon=True).start()
                while True:
                    item = ev_q.get()
                    if item is None:
                        break
                    try:
                        _emit(item)
                    except Exception:
                        _end_chunks()
                        return
                if holder.get("error"):
                    try:
                        _emit(
                            {
                                "type": "error",
                                "error": holder["error"],
                                "status_line": STATE.status_line(),
                            }
                        )
                    except Exception:
                        pass
                    _end_chunks()
                    return
                result = holder.get("result")
                try:
                    _emit(
                        {
                            "type": "done",
                            "answer": getattr(result, "answer", "") or "",
                            "source": getattr(result, "source", "") or "",
                            "status_line": STATE.status_line(),
                        }
                    )
                except Exception:
                    pass
                _end_chunks()
                return
            if path == "/api/remember":
                question = (data.get("question") or "").strip()
                answer = (data.get("answer") or "").strip()
                scene = (data.get("scene") or "general").strip() or "general"
                tags = data.get("tags")
                if isinstance(tags, str):
                    tags = [tags]
                kind = (data.get("kind") or "").strip() or None
                facts = data.get("facts") if isinstance(data.get("facts"), dict) else None
                if not question or not answer:
                    _json_response(self, 400, {"error": "question/answer 不能为空"})
                    return
                msg = STATE.sandbox.remember(
                    question, answer, scene=scene, tags=tags, kind=kind, facts=facts
                )
                _json_response(
                    self,
                    200,
                    {"message": msg, "status_line": STATE.status_line()},
                )
                return
            if path == "/api/extract":
                text = data.get("text") or ""
                if not str(text).strip():
                    _json_response(self, 400, {"error": "text 不能为空"})
                    return
                tags = data.get("tags")
                if isinstance(tags, str):
                    tags = [tags]
                try:
                    max_n = int(data.get("max_n") or 3)
                except (TypeError, ValueError):
                    max_n = 3
                payload = STATE.sandbox.extract_candidates(
                    str(text), max_n=max(1, min(max_n, 8)), tags=tags
                )
                payload["status_line"] = STATE.status_line()
                _json_response(self, 200, payload)
                return
            if path == "/api/export_pack":
                filter_tags = data.get("filter_tags") or data.get("tags")
                if isinstance(filter_tags, str):
                    filter_tags = [filter_tags]
                try:
                    limit = int(data.get("limit") or 500)
                except (TypeError, ValueError):
                    limit = 500
                msg = STATE.sandbox.export_pack(
                    name=(data.get("name") or "memory-pack").strip() or "memory-pack",
                    dest=(data.get("dest") or "").strip() or None,
                    description=(data.get("description") or "").strip(),
                    filter_tags=filter_tags,
                    filter_scene=(data.get("filter_scene") or data.get("scene") or "").strip()
                    or None,
                    limit=max(1, min(limit, 5000)),
                )
                _json_response(
                    self, 200, {"message": msg, "status_line": STATE.status_line()}
                )
                return
            if path == "/api/import_pack":
                path_in = (data.get("path") or "").strip()
                if not path_in:
                    _json_response(self, 400, {"error": "path 不能为空"})
                    return
                merge = data.get("merge")
                if merge is None:
                    merge = True
                msg = STATE.sandbox.import_pack(
                    path_in, merge=bool(merge), confirm=bool(data.get("confirm"))
                )
                _json_response(
                    self, 200, {"message": msg, "status_line": STATE.status_line()}
                )
                return
            if path == "/api/archive":
                msg = STATE.sandbox.archive_stale(
                    min_hits=data.get("min_hits"),
                    older_than_days=data.get("older_than_days"),
                    confirm=bool(data.get("confirm")),
                )
                _json_response(
                    self, 200, {"message": msg, "status_line": STATE.status_line()}
                )
                return
            if path == "/api/git_check":
                try:
                    limit = int(data.get("limit") or 8)
                except (TypeError, ValueError):
                    limit = 8
                payload = STATE.sandbox.check_git_changes(
                    cwd=(data.get("cwd") or "").strip() or None,
                    since_ref=(data.get("since_ref") or "HEAD~20").strip() or "HEAD~20",
                    limit=max(1, min(limit, 30)),
                )
                payload["status_line"] = STATE.status_line()
                _json_response(self, 200, payload)
                return
            if path == "/api/list_packs":
                payload = STATE.sandbox.list_packs()
                payload["status_line"] = STATE.status_line()
                _json_response(self, 200, payload)
                return
            if path == "/api/delete_memory":
                memory_id = (data.get("id") or data.get("memory_id") or "").strip()
                question = (data.get("question") or "").strip()
                msg = STATE.sandbox.delete_memory(memory_id=memory_id, question=question)
                ok = not msg.startswith("未找到") and not msg.startswith("请提供")
                _json_response(
                    self,
                    200,
                    {
                        "message": msg,
                        "ok": ok,
                        "error": None if ok else msg,
                        "status_line": STATE.status_line(),
                    },
                )
                return
            if path == "/api/status":
                _json_response(
                    self,
                    200,
                    {"status": STATE.sandbox.status(), "status_line": STATE.status_line()},
                )
                return
            if path == "/api/agent_mode":
                mode = (data.get("mode") or data.get("agent_mode") or "").strip()
                persist = data.get("persist", True)
                if not mode:
                    st = STATE.sandbox.status()
                    _json_response(
                        self,
                        200,
                        {
                            "agent_mode": (st.get("llm") or {}).get("agent_mode"),
                            "agent_force": (st.get("llm") or {}).get("agent_force"),
                            "status_line": STATE.status_line(),
                        },
                    )
                    return
                try:
                    msg = STATE.sandbox.set_agent_mode(mode, persist=bool(persist))
                except ValueError as e:
                    _json_response(self, 400, {"error": str(e), "status_line": STATE.status_line()})
                    return
                st = STATE.sandbox.status()
                _json_response(
                    self,
                    200,
                    {
                        "message": msg,
                        "agent_mode": (st.get("llm") or {}).get("agent_mode"),
                        "agent_force": (st.get("llm") or {}).get("agent_force"),
                        "status_line": STATE.status_line(),
                    },
                )
                return
            if path == "/api/list_memory":
                layer = (data.get("layer") or "all").strip()
                text = STATE.sandbox.format_memory_view(layer)
                payload = {
                    "layer": layer,
                    "text": text,
                    "status_line": STATE.status_line(),
                }
                if layer in ("working", "short"):
                    payload["items"] = STATE.sandbox.list_working()
                elif layer in ("long_term", "long"):
                    payload["data"] = STATE.sandbox.list_long_term()
                else:
                    payload["working"] = STATE.sandbox.list_working()
                    payload["long_term"] = STATE.sandbox.list_long_term()
                _json_response(self, 200, payload)
                return
            if path == "/api/clear_working":
                STATE.sandbox.working.clear()
                _json_response(
                    self,
                    200,
                    {"message": "工作记忆已清空。", "status_line": STATE.status_line()},
                )
                return
            if path == "/api/backup_long_term":
                dest = (data.get("dest") or "").strip() or None
                msg = STATE.sandbox.backup_long_term(dest)
                backups = [str(p) for p in STATE.sandbox.long_term.list_backups()[:10]]
                _json_response(
                    self,
                    200,
                    {
                        "message": msg,
                        "backups": backups,
                        "status_line": STATE.status_line(),
                    },
                )
                return
            if path == "/api/clear_long_term":
                if not data.get("confirm"):
                    STATE.sandbox.long_term.reload()
                    n = len(STATE.sandbox.long_term.records)
                    _json_response(
                        self,
                        400,
                        {
                            "error": "需要确认：请传 confirm=true",
                            "needs_confirm": True,
                            "count": n,
                            "status_line": STATE.status_line(),
                        },
                    )
                    return
                msg = STATE.sandbox.clear_long_term(
                    backup_first=bool(data.get("backup_first"))
                )
                _json_response(
                    self,
                    200,
                    {
                        "message": msg,
                        "status_line": STATE.status_line(),
                    },
                )
                return
            if path == "/api/seed":
                samples = [
                    ("如何启动本地前端", "在项目根目录执行 pnpm install && pnpm start，注意检查 .npmrc 私源配置。"),
                    ("agency 项目怎么跑", "进入 live_web_agency，执行 pnpm install，再 pnpm start；e2e 用 agency-e2e。"),
                    ("切换开发环境要注意什么", "确认当前 Node/pnpm 版本、hosts/代理、环境变量（.env）以及对应业务的 mock 开关。"),
                    ("记忆沙箱怎么减少 token", "优先把高频问答用「记住：问 => 答」写入长时记忆；重复问题会直接命中沙箱，不走大模型。"),
                    ("git 提交规范", "使用简洁祈使句说明 why；不要自动 push；不要改 git config。"),
                ]
                for q, a in samples:
                    STATE.sandbox.remember(q, a, scene="dev")
                STATE.sandbox.working.set_scene("dev")
                _json_response(
                    self,
                    200,
                    {
                        "message": f"已写入 {len(samples)} 条开发场景记忆，当前场景: dev",
                        "status_line": STATE.status_line(),
                    },
                )
                return
            if path == "/api/open_data":
                path_dir = app_support_dir()
                path_dir.mkdir(parents=True, exist_ok=True)
                if sys.platform == "darwin":
                    os.system(f'open "{path_dir}"')
                _json_response(
                    self,
                    200,
                    {"message": f"已打开数据目录：{path_dir}", "path": str(path_dir)},
                )
                return
            if path == "/api/shutdown":
                _json_response(self, 200, {"ok": True, "message": "正在退出"})

                def _stop():
                    try:
                        if SERVER is not None:
                            SERVER.shutdown()
                    finally:
                        os._exit(0)

                threading.Timer(0.15, _stop).start()
                return
            self.send_error(404)
        except Exception as e:
            _json_response(
                self,
                500,
                {"error": str(e), "trace": traceback.format_exc()},
            )


def find_free_port(start: int = PREFERRED_PORT, span: int = 20) -> int:
    for port in range(start, start + span):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((HOST, port))
                return port
            except OSError:
                continue
    raise RuntimeError("无可用本地端口")


def probe_running_sandbox(
    start: int = PREFERRED_PORT, span: int = 20
) -> Optional[dict]:
    """探测本机是否已有记忆沙箱 Web 服务。"""
    for port in range(start, start + span):
        health = f"http://{HOST}:{port}/api/health"
        try:
            with urllib.request.urlopen(health, timeout=0.35) as resp:
                if resp.status != 200:
                    continue
                data = json.loads(resp.read().decode("utf-8") or "{}")
            if data.get("ok") and data.get("app", "memory-sandbox") == "memory-sandbox":
                return {
                    "url": f"http://{HOST}:{port}/",
                    "port": port,
                    "build": data.get("build") or "",
                    "features": list(data.get("features") or []),
                }
        except Exception:
            continue
    return None


def find_running_sandbox(start: int = PREFERRED_PORT, span: int = 20) -> Optional[str]:
    """兼容旧调用：只返回页面 URL。"""
    info = probe_running_sandbox(start=start, span=span)
    return info["url"] if info else None


def request_shutdown_sandbox(base_url: str) -> bool:
    """请求已运行实例退出。"""
    endpoint = base_url.rstrip("/") + "/api/shutdown"
    try:
        req = urllib.request.Request(
            endpoint,
            data=b"{}",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            _ = resp.read()
        return True
    except Exception:
        return False


def wait_until_port_free(port: int, timeout_s: float = 4.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.2)
            try:
                s.connect((HOST, port))
            except OSError:
                return True
        time.sleep(0.15)
    return False


def force_free_port(port: int) -> None:
    """shutdown 失败时，按端口清理监听进程（仅本机开发/桌面场景）。"""
    if sys.platform != "darwin" and sys.platform != "linux":
        return
    try:
        proc = subprocess.run(
            ["lsof", "-ti", f"tcp:{port}", f"-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        pids = [p.strip() for p in (proc.stdout or "").splitlines() if p.strip()]
        for pid in pids:
            try:
                os.kill(int(pid), 15)
            except Exception:
                pass
        wait_until_port_free(port, timeout_s=2.0)
    except Exception:
        pass


def _macos_refresh_tab(url_prefix: str) -> bool:
    """在常见浏览器中查找已打开的沙箱页并 reload，避免新开标签。"""
    prefix = url_prefix.rstrip("/")
    # AppleScript：Chrome 系 / Safari；任一成功即返回
    script = f'''
on run
  set prefix to "{prefix}"
  set apps to {{"Google Chrome", "Chromium", "Microsoft Edge", "Brave Browser", "Arc", "Safari"}}
  repeat with appName in apps
    try
      if application appName is running then
        if appName is "Safari" then
          tell application "Safari"
            repeat with w in windows
              repeat with t in tabs of w
                if (URL of t as string) starts with prefix then
                  set current tab of w to t
                  set index of w to 1
                  set u to URL of t
                  set URL of t to u
                  activate
                  return "ok"
                end if
              end repeat
            end repeat
          end tell
        else
          tell application appName
            repeat with w in windows
              set i to 0
              repeat with t in tabs of w
                set i to i + 1
                if (URL of t as string) starts with prefix then
                  set active tab index of w to i
                  set index of w to 1
                  tell t to reload
                  activate
                  return "ok"
                end if
              end repeat
            end repeat
          end tell
        end if
      end if
    end try
  end repeat
  return "miss"
end run
'''
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=8,
        )
        return proc.returncode == 0 and (proc.stdout or "").strip() == "ok"
    except Exception:
        return False


def open_or_refresh_ui(url: str) -> str:
    """优先刷新已打开标签；否则新开。返回 refreshed | opened。"""
    prefix = url.rstrip("/")
    if sys.platform == "darwin" and _macos_refresh_tab(prefix):
        return "refreshed"
    # new=0：尽量同窗口；多数浏览器仍可能新开标签，故 macOS 优先走上面的 refresh
    webbrowser.open(url, new=0, autoraise=True)
    return "opened"


def notify_mac(title: str, message: str):
    if sys.platform != "darwin":
        return
    safe_title = title.replace('"', '\\"')
    safe_msg = message.replace('"', '\\"')
    os.system(
        f'osascript -e \'display notification "{safe_msg}" with title "{safe_title}"\' >/dev/null 2>&1'
    )


def main():
    global STATE, SERVER
    if is_frozen():
        try:
            os.chdir(app_support_dir())
        except Exception:
            pass

    # 已有实例：若含思考流则只刷新；否则关掉旧进程再起新服务
    existing = probe_running_sandbox()
    if existing and "chat_stream" in (existing.get("features") or []):
        action = open_or_refresh_ui(existing["url"])
        msg = (
            f"已刷新已打开页面 {existing['url']} (build {existing.get('build') or '?'})"
            if action == "refreshed"
            else f"已唤起界面 {existing['url']}（未新启服务）"
        )
        notify_mac("记忆沙箱", msg)
        print(msg)
        return

    if existing:
        print(
            f"检测到旧版服务 {existing['url']}（无 chat_stream），正在重启以显示思考过程…"
        )
        request_shutdown_sandbox(existing["url"])
        if not wait_until_port_free(int(existing["port"]), timeout_s=3.0):
            force_free_port(int(existing["port"]))
        # 再确认旧实例已消失；若仍在且仍无流能力，继续强制占端口启动会失败，故再清一次
        again = probe_running_sandbox()
        if again and "chat_stream" not in (again.get("features") or []):
            force_free_port(int(again["port"]))

    STATE = AppState()
    port = find_free_port()
    SERVER = ThreadingHTTPServer((HOST, port), Handler)
    url = f"http://{HOST}:{port}/"

    def _open():
        open_or_refresh_ui(url)

    threading.Timer(0.4, _open).start()
    notify_mac("记忆沙箱", f"已在浏览器打开 {url}，退出请点页面「退出应用」")
    print(f"记忆沙箱 Web UI: {url}  build={UI_BUILD}  features={list(UI_FEATURES)}")

    try:
        SERVER.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        SERVER.server_close()


if __name__ == "__main__":
    main()
