import { FormEvent, useState } from "react";
import { Loader2, LockKeyhole, LogIn, MonitorCog } from "lucide-react";

type LoginPageProps = {
  loading: boolean;
  error: string | null;
  onLogin: (loginIdentifier: string, password: string) => Promise<void>;
};

export function LoginPage({ loading, error, onLogin }: LoginPageProps) {
  const [loginIdentifier, setLoginIdentifier] = useState("demo@example.com");
  const [password, setPassword] = useState("demo-password");

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    await onLogin(loginIdentifier, password);
  }

  return (
    <main className="login-shell">
      <section className="login-panel">
        <div className="login-brand">
          <span>
            <MonitorCog size={22} />
          </span>
          <div>
            <strong>PC Agent</strong>
            <small>客服工作台</small>
          </div>
        </div>

        <form className="login-form" onSubmit={handleSubmit}>
          <label>
            登录标识
            <input
              value={loginIdentifier}
              onChange={(event) => setLoginIdentifier(event.target.value)}
              disabled={loading}
              autoComplete="username"
            />
          </label>
          <label>
            密码
            <input
              type="password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              disabled={loading}
              autoComplete="current-password"
            />
          </label>
          {error && (
            <div className="login-error">
              <LockKeyhole size={16} />
              <span>{error}</span>
            </div>
          )}
          <button
            type="submit"
            disabled={loading || !loginIdentifier.trim() || password.length < 8}
          >
            {loading ? <Loader2 size={18} className="spin" /> : <LogIn size={18} />}
            登录
          </button>
        </form>
      </section>
    </main>
  );
}
