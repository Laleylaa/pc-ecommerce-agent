import {
  Loader2,
  LogOut,
  MessageSquarePlus,
  MessagesSquare,
  Plus,
  Trash2,
  UserRound
} from "lucide-react";
import { useState } from "react";
import { formatClock } from "./common";
import type { ConversationSummary, OperatorProfile } from "../types";

type SidebarProps = {
  operator: OperatorProfile;
  conversations: ConversationSummary[];
  activeConversationId?: number;
  conversationsLoading: boolean;
  disabled: boolean;
  onLogout: () => void;
  onNewConversation: () => void;
  onSelectConversation: (conversationId: number) => void;
  onDeleteConversation: (conversationId: number) => void;
};

function groupConversations(conversations: ConversationSummary[]) {
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const yesterday = new Date(today.getTime() - 86400000);

  const groups: { label: string; conversations: ConversationSummary[] }[] = [
    { label: "今天", conversations: [] },
    { label: "昨天", conversations: [] },
    { label: "更早", conversations: [] },
  ];

  for (const conv of conversations) {
    const date = new Date(conv.last_message_at ?? conv.created_at);
    if (date >= today) {
      groups[0].conversations.push(conv);
    } else if (date >= yesterday) {
      groups[1].conversations.push(conv);
    } else {
      groups[2].conversations.push(conv);
    }
  }

  return groups.filter((group) => group.conversations.length > 0);
}

export function Sidebar({
  operator,
  conversations,
  activeConversationId,
  conversationsLoading,
  disabled,
  onLogout,
  onNewConversation,
  onSelectConversation,
  onDeleteConversation
}: SidebarProps) {
  return (
    <aside className="sidebar">
      <div className="sidebar-header">
        <div className="brand-row">
          <span className="brand-mark">
            <MessageSquarePlus size={16} />
          </span>
          <div>
            <strong>PC Agent</strong>
            <small>客服工作台</small>
          </div>
        </div>

        <button
          type="button"
          className="new-chat-button"
          onClick={onNewConversation}
          disabled={disabled}
        >
          <Plus size={16} />
          新会话
        </button>
      </div>

      <div className="operator-compact">
        <span className="operator-avatar">
          <UserRound size={15} />
        </span>
        <span className="operator-name">{operator.name}</span>
        <span className="auth-chip">{operator.statusLabel}</span>
        <button type="button" className="logout-link" onClick={onLogout} disabled={disabled}>
          <LogOut size={14} />
        </button>
      </div>

      <section className="conversation-section">
        <div className="conversation-section-head">
          <h2>会话</h2>
        </div>
        <div className="conversation-list">
          {groupConversations(conversations).map((group) => (
            <div key={group.label} className="conversation-group">
              <h3 className="conversation-group-label">{group.label}</h3>
              {group.conversations.map((conversation) => (
                <ConversationRow
                  key={conversation.id}
                  conversation={conversation}
                  active={conversation.id === activeConversationId}
                  disabled={disabled}
                  onSelect={onSelectConversation}
                  onDelete={onDeleteConversation}
                />
              ))}
            </div>
          ))}
          {conversationsLoading && (
            <div className="conversation-loading">
              <Loader2 size={15} className="spin" />
              <span>加载中</span>
            </div>
          )}
          {!conversations.length && !conversationsLoading && (
            <div className="conversation-empty">暂无历史会话</div>
          )}
        </div>
      </section>
    </aside>
  );
}

function ConversationRow({
  conversation,
  active,
  disabled,
  onSelect,
  onDelete
}: {
  conversation: ConversationSummary;
  active: boolean;
  disabled: boolean;
  onSelect: (id: number) => void;
  onDelete: (id: number) => void;
}) {
  const [confirming, setConfirming] = useState(false);

  if (confirming) {
    return (
      <div className="conversation-item delete-confirm">
        <span>确认删除？</span>
        <div className="confirm-actions">
          <button
            type="button"
            className="confirm-yes"
            onClick={() => {
              onDelete(conversation.id);
              setConfirming(false);
            }}
            disabled={disabled}
          >
            删除
          </button>
          <button
            type="button"
            className="confirm-no"
            onClick={() => setConfirming(false)}
          >
            取消
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className={`conversation-item ${active ? "active" : ""}`}>
      <button
        type="button"
        className="conversation-select"
        disabled={disabled}
        onClick={() => onSelect(conversation.id)}
      >
        <MessagesSquare size={15} />
        <span>
          <strong>{conversation.title}</strong>
          <small>
            {conversation.last_message ?? "暂无消息"}
            {conversation.last_message_at ? ` · ${formatClock(conversation.last_message_at)}` : ""}
          </small>
        </span>
      </button>
      <button
        type="button"
        className="conversation-delete"
        onClick={() => setConfirming(true)}
        disabled={disabled}
        aria-label="删除会话"
      >
        <Trash2 size={14} />
      </button>
    </div>
  );
}