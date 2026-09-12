import React, { useState } from 'react';
import { Radio, Lock, Mail, ArrowRight, ShieldCheck, AlertCircle } from 'lucide-react';
import { useAuth } from '../context/AuthContext';

export const LoginScreen: React.FC = () => {
  const { signIn } = useAuth();
  const [email, setEmail] = useState('drone.army@gmail.com');
  const [password, setPassword] = useState('');
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setErrorMsg(null);
    if (!email || !password) {
      setErrorMsg('Please enter both email and password');
      return;
    }

    setIsSubmitting(true);
    const { error } = await signIn(email.trim(), password);
    setIsSubmitting(false);

    if (error) {
      setErrorMsg(error.message);
    }
  };

  return (
    <div className="min-h-screen bg-bg flex flex-col justify-center items-center px-4 py-8 relative overflow-hidden">
      {/* Tactical background grid & glow */}
      <div className="absolute inset-0 bg-[radial-gradient(#1f262e_1px,transparent_1px)] [background-size:24px_24px] opacity-40 pointer-events-none" />
      <div className="absolute top-1/4 left-1/2 -translate-x-1/2 w-96 h-96 bg-tactical-cyan/5 rounded-full blur-3xl pointer-events-none" />

      <div className="w-full max-w-md relative z-10">
        {/* Header / Logo */}
        <div className="text-center mb-8">
          <div className="inline-flex items-center justify-center w-16 h-16 rounded-2xl bg-bg-card border border-tactical-cyan/40 shadow-[0_0_20px_rgba(0,229,255,0.2)] text-tactical-cyan mb-4">
            <Radio className="w-8 h-8 animate-pulse" />
          </div>
          <h1 className="text-2xl font-black tracking-wider text-white">
            GSS GROUND STATION
          </h1>
          <p className="text-xs text-gray-400 mt-1 uppercase tracking-widest">
            Autonomous Security Aircraft Control System
          </p>
        </div>

        {/* Login Card */}
        <div className="bg-bg-card border border-bg-line rounded-2xl p-6 sm:p-8 shadow-2xl backdrop-blur-sm">
          <div className="flex items-center gap-2 mb-6 pb-4 border-b border-bg-line">
            <ShieldCheck className="w-5 h-5 text-tactical-ok" />
            <span className="text-xs font-mono uppercase tracking-wider text-gray-300">
              Single-Operator Authentication
            </span>
          </div>

          {errorMsg && (
            <div className="mb-5 p-3.5 rounded-xl bg-tactical-bad/15 border border-tactical-bad/40 text-tactical-bad text-xs flex items-start gap-2.5">
              <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" />
              <span>{errorMsg}</span>
            </div>
          )}

          <form onSubmit={handleSubmit} className="space-y-4">
            <div>
              <label
                htmlFor="email"
                className="block text-xs font-semibold text-gray-300 uppercase tracking-wider mb-1.5"
              >
                Operator Email
              </label>
              <div className="relative">
                <div className="absolute inset-y-0 left-0 pl-3.5 flex items-center pointer-events-none text-gray-500">
                  <Mail className="w-4 h-4" />
                </div>
                <input
                  id="email"
                  type="email"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  placeholder="drone.army@gmail.com"
                  autoComplete="username"
                  required
                  className="w-full pl-10 pr-4 py-2.5 rounded-xl bg-bg-input border border-bg-line text-white text-sm focus:outline-none focus:border-tactical-cyan transition-colors"
                />
              </div>
            </div>

            <div>
              <label
                htmlFor="password"
                className="block text-xs font-semibold text-gray-300 uppercase tracking-wider mb-1.5"
              >
                Operator Password
              </label>
              <div className="relative">
                <div className="absolute inset-y-0 left-0 pl-3.5 flex items-center pointer-events-none text-gray-500">
                  <Lock className="w-4 h-4" />
                </div>
                <input
                  id="password"
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder="••••••••••••"
                  autoComplete="current-password"
                  required
                  className="w-full pl-10 pr-4 py-2.5 rounded-xl bg-bg-input border border-bg-line text-white text-sm focus:outline-none focus:border-tactical-cyan transition-colors"
                />
              </div>
            </div>

            <button
              type="submit"
              disabled={isSubmitting}
              className="w-full mt-2 flex items-center justify-center gap-2 py-3 px-4 rounded-xl bg-tactical-blue hover:bg-tactical-blue/90 text-white font-bold text-sm uppercase tracking-wider shadow-lg shadow-tactical-blue/20 transition-all disabled:opacity-50 active:scale-98"
            >
              {isSubmitting ? (
                <span className="inline-block w-4 h-4 border-2 border-white/20 border-t-white rounded-full animate-spin" />
              ) : (
                <>
                  <span>Sign In to Ground Station</span>
                  <ArrowRight className="w-4 h-4" />
                </>
              )}
            </button>
          </form>

          <div className="mt-6 pt-4 border-t border-bg-line/60 text-center">
            <span className="text-[11px] text-gray-500 font-mono">
              Patna HQ • Agamkuan Ground Station
            </span>
          </div>
        </div>
      </div>
    </div>
  );
};
