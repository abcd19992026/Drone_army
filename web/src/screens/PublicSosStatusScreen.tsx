import React, { useState, useEffect } from 'react';
import {
  PhoneCall,
  ShieldAlert,
  Clock,
  MapPin,
  VideoOff,
  RefreshCw,
  AlertTriangle,
  Radio,
  CheckCircle2,
  Navigation2,
  HelpCircle,
} from 'lucide-react';
import { usePublicSosStatus } from '../hooks/usePublicSosStatus';
import { PublicSosMap } from '../components/PublicSosMap';
import { formatFullDateTime, formatLiveElapsedTime } from '../lib/utils';
import type { SosPlainStatus } from '../types';

interface PublicSosStatusScreenProps {
  commandId: string;
}

export const PublicSosStatusScreen: React.FC<PublicSosStatusScreenProps> = ({ commandId }) => {
  const {
    status,
    loading,
    isExpiredOrNotFound,
    lastFetchedAt,
    refetch,
  } = usePublicSosStatus(commandId);

  // Live elapsed time tick every 1 second
  const [nowMs, setNowMs] = useState<number>(Date.now());
  const [isManualRefreshing, setIsManualRefreshing] = useState<boolean>(false);

  useEffect(() => {
    const timer = setInterval(() => {
      setNowMs(Date.now());
    }, 1000);
    return () => clearInterval(timer);
  }, []);

  const handleManualRefresh = async () => {
    setIsManualRefreshing(true);
    await refetch();
    setTimeout(() => setIsManualRefreshing(false), 500);
  };

  // 1. Initial Loading State
  if (loading && !status) {
    return (
      <div className="min-h-screen bg-bg flex flex-col items-center justify-center p-4 text-center">
        <div className="relative mb-4">
          <div className="w-16 h-16 rounded-full border-3 border-tactical-bad border-t-transparent animate-spin" />
          <ShieldAlert className="w-7 h-7 absolute inset-0 m-auto text-tactical-bad animate-pulse" />
        </div>
        <h2 className="text-base font-bold text-white uppercase tracking-wider mb-1">
          Loading Emergency SOS Status...
        </h2>
        <p className="text-xs text-gray-400 font-mono">
          Connecting to Autonomous Incident Tracking
        </p>
      </div>
    );
  }

  // 2. Expired / Invalid / Not Found State
  if (isExpiredOrNotFound || !status) {
    return (
      <div className="min-h-screen bg-bg text-gray-100 flex flex-col p-4 sm:p-6 max-w-lg mx-auto justify-center">
        <div className="bg-bg-card border-2 border-bg-line rounded-3xl p-6 sm:p-8 shadow-2xl space-y-6 text-center">
          <div className="w-16 h-16 rounded-full bg-tactical-warn/20 border-2 border-tactical-warn/40 flex items-center justify-center mx-auto text-tactical-warn">
            <Clock className="w-8 h-8" />
          </div>

          <div className="space-y-2">
            <h1 className="text-xl sm:text-2xl font-black text-white tracking-tight">
              Link No Longer Active
            </h1>
            <p className="text-xs sm:text-sm text-gray-300 leading-relaxed">
              This emergency tracking link has expired or the incident is closed. For privacy and security, public SOS tracking links are automatically time-boxed up to 48 hours.
            </p>
          </div>

          {/* Prominent Call Police Option */}
          <div className="pt-2 space-y-3">
            <a
              href="tel:112"
              className="w-full py-4 px-6 rounded-2xl bg-gradient-to-r from-red-600 via-tactical-bad to-red-600 hover:from-red-500 hover:to-red-500 text-white font-black text-lg shadow-[0_0_25px_rgba(224,83,61,0.4)] border-2 border-red-400 flex items-center justify-center gap-3 transition-all active:scale-98 cursor-pointer"
            >
              <PhoneCall className="w-6 h-6 animate-bounce" />
              <span>CALL POLICE (112)</span>
            </a>
            <p className="text-[11px] text-gray-500 font-mono">
              India National Emergency Helpline (Toll Free)
            </p>
          </div>

          <div className="border-t border-bg-line pt-4 text-center">
            <button
              onClick={handleManualRefresh}
              disabled={isManualRefreshing}
              className="text-xs text-tactical-cyan hover:underline flex items-center justify-center gap-1.5 mx-auto cursor-pointer"
            >
              <RefreshCw className={`w-3.5 h-3.5 ${isManualRefreshing ? 'animate-spin' : ''}`} />
              <span>Check again</span>
            </button>
          </div>
        </div>
      </div>
    );
  }

  // 3. Active SOS Incident Screen
  const { text: elapsedText } = formatLiveElapsedTime(status.command_created_at, nowMs);

  // Helper to determine plain status presentation
  const getStatusPresentation = (plainStatus: SosPlainStatus, reason: string | null) => {
    switch (plainStatus) {
      case 'dispatched':
        return {
          title: 'Drone Dispatched & Launching',
          description: 'The autonomous drone has received the emergency signal and is launching toward the target location.',
          colorClass: 'text-tactical-warn border-tactical-warn bg-tactical-warn/15',
          badgeVariant: 'DISPATCHED',
          icon: <Radio className="w-5 h-5 animate-pulse text-tactical-warn" />,
        };
      case 'enroute':
        return {
          title: 'Drone En Route to Location',
          description: 'The drone is airborne and actively flying towards the person’s coordinates to provide visual and beacon support.',
          colorClass: 'text-tactical-cyan border-tactical-cyan bg-tactical-cyan/15',
          badgeVariant: 'EN ROUTE',
          icon: <Navigation2 className="w-5 h-5 animate-pulse text-tactical-cyan" />,
        };
      case 'returned':
        return {
          title: 'Mission Completed',
          description: 'The security drone completed its emergency response flight and returned safely to the charging dock.',
          colorClass: 'text-tactical-ok border-tactical-ok bg-tactical-ok/15',
          badgeVariant: 'RESOLVED',
          icon: <CheckCircle2 className="w-5 h-5 text-tactical-ok" />,
        };
      case 'not_dispatched':
      default:
        if (reason) {
          return {
            title: 'Drone Could Not Be Dispatched',
            description: `Auto-dispatch unavailable: ${reason}. Emergency authorities should be notified immediately.`,
            colorClass: 'text-tactical-bad border-tactical-bad bg-tactical-bad/15',
            badgeVariant: 'STANDBY',
            icon: <AlertTriangle className="w-5 h-5 text-tactical-bad animate-pulse" />,
          };
        }
        return {
          title: 'SOS Alert Registered',
          description: 'Emergency command received at ground station. System is processing flight path and vehicle readiness.',
          colorClass: 'text-tactical-warn border-tactical-warn bg-tactical-warn/15',
          badgeVariant: 'PROCESSING',
          icon: <Clock className="w-5 h-5 text-tactical-warn animate-pulse" />,
        };
    }
  };

  const statusInfo = getStatusPresentation(status.plain_status, status.reason);

  return (
    <div className="min-h-screen bg-bg text-gray-100 flex flex-col selection:bg-tactical-bad/30 selection:text-white pb-28">
      {/* Top Emergency Header */}
      <header className="sticky top-0 z-40 bg-bg-card/95 backdrop-blur-md border-b border-bg-line px-4 py-3 shadow-lg">
        <div className="max-w-xl mx-auto flex items-center justify-between gap-3">
          <div className="flex items-center gap-2.5">
            <div className="w-9 h-9 rounded-xl bg-tactical-bad/20 border border-tactical-bad/40 flex items-center justify-center text-tactical-bad">
              <ShieldAlert className="w-5 h-5 animate-pulse" />
            </div>
            <div>
              <div className="flex items-center gap-2">
                <h1 className="text-sm font-black tracking-wider text-white uppercase">
                  EMERGENCY SOS TRACKING
                </h1>
              </div>
              <p className="text-[11px] text-gray-400 font-mono">
                Live Incident Monitor • Public Access
              </p>
            </div>
          </div>

          <div className="flex items-center gap-2">
            <button
              onClick={handleManualRefresh}
              disabled={isManualRefreshing}
              title="Refresh Live Status"
              className="p-2 rounded-lg bg-bg-cardElevated hover:bg-bg-line text-gray-300 hover:text-white border border-bg-line transition-all active:scale-95 cursor-pointer"
            >
              <RefreshCw className={`w-4 h-4 ${isManualRefreshing ? 'animate-spin text-tactical-cyan' : ''}`} />
            </button>
          </div>
        </div>
      </header>

      {/* Main Content Area (Mobile First Max Width) */}
      <main className="flex-1 max-w-xl mx-auto w-full px-4 py-4 space-y-4">
        {/* 1. PRIMARY PROMINENT CALL POLICE 112 BUTTON */}
        <section className="bg-bg-card border-2 border-tactical-bad/60 rounded-3xl p-4 sm:p-5 shadow-2xl shadow-tactical-bad/20 relative overflow-hidden">
          <div className="absolute -top-10 -right-10 w-32 h-32 bg-tactical-bad/10 rounded-full blur-2xl pointer-events-none" />

          <a
            href="tel:112"
            className="w-full py-4 px-5 rounded-2xl bg-gradient-to-r from-red-600 via-tactical-bad to-red-600 hover:from-red-500 hover:to-red-500 text-white font-black text-lg sm:text-xl shadow-[0_0_25px_rgba(224,83,61,0.5)] border-2 border-red-400 flex items-center justify-center gap-3 transition-all active:scale-98 animate-pulse-urgent tracking-wide text-center cursor-pointer"
          >
            <PhoneCall className="w-6 h-6 animate-bounce" />
            <span>CALL POLICE (112)</span>
          </a>

          <div className="mt-2.5 flex items-center justify-between text-[11px] text-gray-400 font-mono px-1">
            <span>India National Emergency Services</span>
            <span className="text-tactical-bad font-bold">TOLL FREE • 24/7</span>
          </div>
        </section>

        {/* 2. PLAIN-LANGUAGE STATUS BANNER */}
        <section className={`border rounded-2xl p-4.5 sm:p-5 shadow-xl space-y-2 ${statusInfo.colorClass}`}>
          <div className="flex items-center justify-between gap-2">
            <div className="flex items-center gap-2">
              {statusInfo.icon}
              <h2 className="text-base font-bold text-white tracking-wide">
                {statusInfo.title}
              </h2>
            </div>
            <span className="text-[10px] font-mono font-black uppercase px-2 py-0.5 rounded bg-black/40 border border-current">
              {statusInfo.badgeVariant}
            </span>
          </div>
          <p className="text-xs text-gray-200 leading-relaxed font-medium">
            {statusInfo.description}
          </p>
        </section>

        {/* 3. ELAPSED TIME LIVE TICKER CARD */}
        <section className="bg-bg-card border border-bg-line rounded-2xl p-4 sm:p-5 shadow-xl space-y-2">
          <div className="flex items-center justify-between text-xs text-gray-400">
            <span className="flex items-center gap-1.5 uppercase font-bold tracking-wider">
              <Clock className="w-4 h-4 text-tactical-cyan" />
              Incident Elapsed Time
            </span>
            <span className="font-mono text-[11px] text-tactical-ok flex items-center gap-1">
              <span className="w-1.5 h-1.5 rounded-full bg-tactical-ok animate-ping" />
              Live Ticker
            </span>
          </div>

          <div className="pt-1">
            <div className="text-2xl sm:text-3xl font-black font-mono tracking-tight text-white">
              {elapsedText}
            </div>
            <div className="text-xs text-gray-400 font-mono mt-1">
              Triggered: {formatFullDateTime(status.command_created_at)}
            </div>
          </div>
        </section>

        {/* 4. CURRENT LOCATION ON INTERACTIVE MAP */}
        <section className="bg-bg-card border border-bg-line rounded-2xl p-4 sm:p-5 shadow-xl space-y-3">
          <div className="flex items-center justify-between pb-2 border-b border-bg-line">
            <h3 className="text-xs font-bold uppercase tracking-wider text-white flex items-center gap-2">
              <MapPin className="w-4 h-4 text-tactical-bad" />
              <span>Location Radar</span>
            </h3>
            <span className="text-[11px] font-mono text-gray-400">
              {status.recent_locations.length > 0 ? 'Live Coordinates' : 'Searching GPS...'}
            </span>
          </div>

          <PublicSosMap
            locations={status.recent_locations}
            plainStatus={status.plain_status}
          />
        </section>

        {/* 5. LIVE VIDEO STREAM PLACEHOLDER (Graceful Future-Proofing) */}
        <section className="bg-bg-card border border-bg-line rounded-2xl p-4 sm:p-5 shadow-xl space-y-3">
          <div className="flex items-center justify-between pb-2 border-b border-bg-line">
            <h3 className="text-xs font-bold uppercase tracking-wider text-white flex items-center gap-2">
              <VideoOff className="w-4 h-4 text-gray-400" />
              <span>Camera Video Feed</span>
            </h3>
            <span className="text-[10px] font-mono font-bold uppercase px-2 py-0.5 rounded bg-bg-input text-gray-400 border border-bg-line">
              OFFLINE
            </span>
          </div>

          <div className="relative w-full aspect-video rounded-xl bg-bg-input border border-bg-line flex flex-col items-center justify-center p-6 text-center overflow-hidden group">
            {/* Viewfinder corner brackets */}
            <div className="absolute top-2 left-2 w-4 h-4 border-t-2 border-l-2 border-gray-600" />
            <div className="absolute top-2 right-2 w-4 h-4 border-t-2 border-r-2 border-gray-600" />
            <div className="absolute bottom-2 left-2 w-4 h-4 border-b-2 border-l-2 border-gray-600" />
            <div className="absolute bottom-2 right-2 w-4 h-4 border-b-2 border-r-2 border-gray-600" />

            <div className="w-12 h-12 rounded-full bg-bg-cardElevated border border-bg-line flex items-center justify-center mb-2.5 text-gray-500">
              <VideoOff className="w-6 h-6" />
            </div>

            <h4 className="text-xs font-bold text-gray-300 uppercase tracking-wider mb-1">
              Live Video Not Available
            </h4>
            <p className="text-[11px] text-gray-400 max-w-xs leading-relaxed font-sans">
              Live video streaming is not currently enabled for this unit. Telemetry, GPS coordinates, and strobe beacon alarms remain fully active.
            </p>
          </div>
        </section>

        {/* 6. EMERGENCY ASSISTANCE TIPS */}
        <section className="bg-bg-cardElevated/50 border border-bg-line/70 rounded-2xl p-4 space-y-2 text-xs">
          <div className="flex items-center gap-1.5 font-bold text-gray-300 uppercase text-[11px] tracking-wider">
            <HelpCircle className="w-4 h-4 text-tactical-cyan" />
            <span>Emergency Guidelines for Trusted Contacts</span>
          </div>
          <ul className="text-gray-400 space-y-1.5 list-disc list-inside text-[11px] leading-relaxed">
            <li>Attempt to call the person immediately to verify their situation.</li>
            <li>If they cannot be reached, dial <strong className="text-white">112</strong> and provide the exact GPS coordinates above.</li>
            <li>Keep this page open — status updates and coordinates refresh automatically.</li>
          </ul>
        </section>

        {/* Footer / Auto-refresh Timestamp */}
        <footer className="text-center text-[11px] font-mono text-gray-500 pt-2">
          {lastFetchedAt && (
            <span>
              Auto-refreshing every 10s • Last update {lastFetchedAt.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false })}
            </span>
          )}
        </footer>
      </main>

      {/* Sticky Bottom Emergency Bar on Mobile */}
      <aside aria-label="Emergency Call Bar" className="fixed bottom-0 left-0 right-0 z-50 bg-bg-card/95 backdrop-blur-lg border-t border-bg-line p-3 sm:hidden shadow-[0_-10px_25px_rgba(0,0,0,0.5)]">
        <a
          href="tel:112"
          className="w-full py-3.5 px-4 rounded-xl bg-gradient-to-r from-red-600 via-tactical-bad to-red-600 text-white font-black text-base shadow-lg shadow-tactical-bad/40 flex items-center justify-center gap-2 active:scale-98 cursor-pointer"
        >
          <PhoneCall className="w-5 h-5 animate-bounce" />
          <span>CALL POLICE (112)</span>
        </a>
      </aside>
    </div>
  );
};
