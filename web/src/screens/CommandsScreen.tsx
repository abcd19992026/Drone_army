import React, { useState } from 'react';
import {
  Send,
  Pause,
  AlertOctagon,
  MapPin,
  CheckCircle2,
  Clock,
  XCircle,
  ShieldAlert,
  HelpCircle,
  Search,
  AlertTriangle,
} from 'lucide-react';
import { WeatherHoldBanner } from '../components/WeatherHoldBanner';
import { Badge } from '../components/Badge';
import { formatDateTime, timeAgo } from '../lib/utils';
import { APP_CONFIG } from '../lib/config';
import type { CommandRow, CommandType, MissionRow } from '../types';

interface CommandsScreenProps {
  weatherHoldMission: MissionRow | null;
  commands: CommandRow[];
  isSending: boolean;
  statusMessage: { text: string; type: 'info' | 'success' | 'error' } | null;
  onIssueCommand: (
    type: CommandType,
    extraParams?: {
      target_lat?: number;
      target_lon?: number;
      target_alt_m?: number;
      params?: Record<string, any>;
      mission_id?: string;
    }
  ) => Promise<{ success: boolean; error?: string }>;
}

export const CommandsScreen: React.FC<CommandsScreenProps> = ({
  weatherHoldMission,
  commands,
  isSending,
  statusMessage,
  onIssueCommand,
}) => {
  const [geoError, setGeoError] = useState<string | null>(null);
  const [geoLocating, setGeoLocating] = useState(false);
  const [showSosModal, setShowSosModal] = useState(false);

  // Shared geolocation acquisition helper
  const acquirePosition = (
    actionName: string,
    onSuccess: (lat: number, lon: number) => Promise<void>
  ) => {
    setGeoError(null);
    if (!navigator.geolocation) {
      setGeoError('Geolocation is not supported by your browser.');
      return;
    }

    setGeoLocating(true);
    navigator.geolocation.getCurrentPosition(
      async (position) => {
        setGeoLocating(false);
        const { latitude, longitude } = position.coords;
        await onSuccess(latitude, longitude);
      },
      (err) => {
        setGeoLocating(false);
        let msg = 'Could not acquire GPS position.';
        if (err.code === err.PERMISSION_DENIED) {
          msg = `Location permission denied. Please enable GPS location to ${actionName}.`;
        } else if (err.code === err.POSITION_UNAVAILABLE) {
          msg = 'GPS location is unavailable.';
        } else if (err.code === err.TIMEOUT) {
          msg = 'GPS location request timed out.';
        }
        setGeoError(msg);
      },
      { enableHighAccuracy: true, timeout: 10000, maximumAge: 0 }
    );
  };

  // 1. Summon Handlers
  const handleSummonHere = () => {
    acquirePosition('summon', async (latitude, longitude) => {
      await onIssueCommand('summon', {
        target_lat: latitude,
        target_lon: longitude,
      });
    });
  };

  const handleSummon20kmBad = async () => {
    // Deliberate test: ~20 km north of dock to verify backend safety geofence rejection
    const badLat = APP_CONFIG.HOME_LAT + 0.18;
    const badLon = APP_CONFIG.HOME_LON;
    await onIssueCommand('summon', {
      target_lat: badLat,
      target_lon: badLon,
    });
  };

  // 2. SOS Handlers
  const handleSosConfirm = () => {
    setShowSosModal(false);
    acquirePosition('send SOS', async (latitude, longitude) => {
      await onIssueCommand('sos', {
        target_lat: latitude,
        target_lon: longitude,
      });
    });
  };

  // 3. Find My Drone Handler
  const handleFindMyDrone = async () => {
    await onIssueCommand('find_my_drone');
  };

  // 4. Weather Hold Handlers
  const handleWeatherContinue = async () => {
    if (!weatherHoldMission) return;
    await onIssueCommand('weather_continue', {
      mission_id: weatherHoldMission.id,
    });
  };

  const handleWeatherRecall = async () => {
    if (!weatherHoldMission) return;
    await onIssueCommand('weather_recall', {
      mission_id: weatherHoldMission.id,
    });
  };

  return (
    <div className="space-y-6 max-w-7xl mx-auto pb-16">
      {/* SOS CONFIRMATION MODAL */}
      {showSosModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/80 backdrop-blur-sm animate-fade-in">
          <div className="bg-bg-card border-2 border-tactical-bad rounded-2xl max-w-md w-full p-6 shadow-2xl space-y-4">
            <div className="flex items-center gap-3 text-tactical-bad">
              <div className="w-10 h-10 rounded-full bg-tactical-bad/20 flex items-center justify-center shrink-0">
                <AlertTriangle className="w-6 h-6 text-tactical-bad animate-pulse" />
              </div>
              <div>
                <h3 className="text-base font-bold text-white uppercase tracking-wider">
                  Confirm Send SOS
                </h3>
                <p className="text-xs text-tactical-bad font-mono">
                  High Priority Emergency Dispatch
                </p>
              </div>
            </div>

            <p className="text-xs text-gray-300 leading-relaxed">
              This will acquire your phone&apos;s current GPS coordinates, dispatch the drone immediately on an emergency mission, activate audio/visual beacons, and broadcast emergency alert notifications.
            </p>

            <div className="flex items-center justify-end gap-3 pt-2">
              <button
                onClick={() => setShowSosModal(false)}
                className="px-4 py-2 rounded-xl bg-bg-cardElevated hover:bg-bg-line text-xs font-semibold text-gray-300 transition-colors"
              >
                Cancel
              </button>
              <button
                onClick={handleSosConfirm}
                disabled={isSending || geoLocating}
                className="px-4 py-2 rounded-xl bg-tactical-bad hover:bg-tactical-bad/90 text-white text-xs font-bold transition-all shadow-lg shadow-tactical-bad/40 flex items-center gap-2 cursor-pointer"
              >
                <ShieldAlert className="w-4 h-4" />
                <span>Confirm & Send SOS</span>
              </button>
            </div>
          </div>
        </div>
      )}

      {/* 1. WEATHER HOLD HERO BANNER (Top Priority) */}
      {weatherHoldMission && (
        <WeatherHoldBanner
          mission={weatherHoldMission}
          onContinue={handleWeatherContinue}
          onRecall={handleWeatherRecall}
          isSending={isSending}
        />
      )}

      {/* 2. COMMAND DISPATCH CARDS */}
      <div className="bg-bg-card border border-bg-line rounded-2xl p-5 sm:p-6 shadow-xl space-y-5">
        <div className="flex items-center justify-between pb-3 border-b border-bg-line">
          <div>
            <h2 className="text-base sm:text-lg font-bold text-white flex items-center gap-2">
              <Send className="w-5 h-5 text-tactical-cyan" />
              <span>COMMAND DISPATCH (INSERT ONLY)</span>
            </h2>
            <p className="text-xs text-gray-400 mt-0.5">
              Client writes go through table <code className="text-tactical-cyan">INSERT</code> only.
              Backend GSS validates safety constraints before executing.
            </p>
          </div>
          <Badge variant="cyan" size="sm">
            RLS ENFORCED
          </Badge>
        </div>

        {/* Status / Feedback message */}
        {statusMessage && (
          <div
            className={`p-3.5 rounded-xl border text-xs font-mono flex items-start gap-2.5 ${
              statusMessage.type === 'error'
                ? 'bg-tactical-bad/15 border-tactical-bad/40 text-tactical-bad'
                : statusMessage.type === 'success'
                ? 'bg-tactical-ok/15 border-tactical-ok/40 text-tactical-ok'
                : 'bg-tactical-blue/15 border-tactical-blue/40 text-tactical-blue'
            }`}
          >
            {statusMessage.type === 'error' ? (
              <XCircle className="w-4 h-4 shrink-0 mt-0.5" />
            ) : statusMessage.type === 'success' ? (
              <CheckCircle2 className="w-4 h-4 shrink-0 mt-0.5" />
            ) : (
              <Clock className="w-4 h-4 shrink-0 mt-0.5" />
            )}
            <span>{statusMessage.text}</span>
          </div>
        )}

        {/* Geolocation Error Alert */}
        {geoError && (
          <div className="p-3.5 rounded-xl bg-tactical-bad/15 border border-tactical-bad/40 text-tactical-bad text-xs flex items-start gap-2">
            <ShieldAlert className="w-4 h-4 shrink-0 mt-0.5" />
            <span>{geoError}</span>
          </div>
        )}

        {/* Action Buttons Grid */}
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5 gap-3.5">
          {/* Summon (Here) */}
          <button
            onClick={handleSummonHere}
            disabled={isSending || geoLocating}
            className="flex flex-col items-center justify-center p-4 rounded-xl bg-tactical-blue hover:bg-tactical-blue/90 text-white font-bold transition-all disabled:opacity-50 shadow-lg shadow-tactical-blue/20 active:scale-98 group"
          >
            <div className="w-10 h-10 rounded-full bg-white/10 flex items-center justify-center mb-2 group-hover:scale-110 transition-transform">
              <MapPin className="w-5 h-5 text-white" />
            </div>
            <span className="text-sm uppercase tracking-wider text-center">
              {geoLocating ? 'Acquiring GPS...' : 'Summon (Here)'}
            </span>
            <span className="text-[10px] font-normal text-blue-200 mt-0.5 text-center">
              Launches drone to phone GPS coords
            </span>
          </button>

          {/* Hold */}
          <button
            onClick={() => onIssueCommand('hold')}
            disabled={isSending}
            className="flex flex-col items-center justify-center p-4 rounded-xl bg-bg-cardElevated hover:bg-bg-line border border-tactical-warn/40 text-tactical-warn font-bold transition-all disabled:opacity-50 active:scale-98 group"
          >
            <div className="w-10 h-10 rounded-full bg-tactical-warn/10 flex items-center justify-center mb-2 group-hover:scale-110 transition-transform">
              <Pause className="w-5 h-5 text-tactical-warn" />
            </div>
            <span className="text-sm uppercase tracking-wider text-center">Hold In Place</span>
            <span className="text-[10px] font-normal text-gray-400 mt-0.5 text-center">
              Pauses trajectory & loiters
            </span>
          </button>

          {/* Emergency Abort */}
          <button
            onClick={() => onIssueCommand('abort')}
            disabled={isSending}
            className="flex flex-col items-center justify-center p-4 rounded-xl bg-tactical-bad/20 hover:bg-tactical-bad/30 border border-tactical-bad text-tactical-bad font-bold transition-all disabled:opacity-50 active:scale-98 group"
          >
            <div className="w-10 h-10 rounded-full bg-tactical-bad/20 flex items-center justify-center mb-2 group-hover:scale-110 transition-transform">
              <AlertOctagon className="w-5 h-5 text-tactical-bad" />
            </div>
            <span className="text-sm uppercase tracking-wider text-center">Abort & Return</span>
            <span className="text-[10px] font-normal text-gray-300 mt-0.5 text-center">
              Failsafe RTL back to dock
            </span>
          </button>

          {/* Find My Drone */}
          <button
            onClick={handleFindMyDrone}
            disabled={isSending}
            className="flex flex-col items-center justify-center p-4 rounded-xl bg-bg-cardElevated hover:bg-bg-line border border-tactical-cyan/40 text-tactical-cyan font-bold transition-all disabled:opacity-50 active:scale-98 group"
          >
            <div className="w-10 h-10 rounded-full bg-tactical-cyan/10 flex items-center justify-center mb-2 group-hover:scale-110 transition-transform">
              <Search className="w-5 h-5 text-tactical-cyan" />
            </div>
            <span className="text-sm uppercase tracking-wider text-center">Find My Drone</span>
            <span className="text-[10px] font-normal text-gray-400 mt-0.5 text-center">
              Sound siren & blink beacon
            </span>
          </button>

          {/* Send SOS Emergency */}
          <button
            onClick={() => setShowSosModal(true)}
            disabled={isSending || geoLocating}
            className="flex flex-col items-center justify-center p-4 rounded-xl bg-tactical-bad hover:bg-tactical-bad/90 text-white font-black transition-all disabled:opacity-50 shadow-lg shadow-tactical-bad/30 border-2 border-red-500 active:scale-98 group cursor-pointer"
          >
            <div className="w-10 h-10 rounded-full bg-white/20 flex items-center justify-center mb-2 group-hover:scale-110 transition-transform">
              <ShieldAlert className="w-5 h-5 text-white animate-pulse" />
            </div>
            <span className="text-sm uppercase tracking-wider text-center flex items-center gap-1.5 font-bold">
              <span>Send SOS</span>
            </span>
            <span className="text-[10px] font-medium text-red-100 mt-0.5 text-center">
              Emergency dispatch to my GPS
            </span>
          </button>
        </div>

        {/* Diagnostics / Test Safety Rejection Button */}
        <div className="pt-2 border-t border-bg-line/50 flex flex-col sm:flex-row items-start sm:items-center justify-between gap-2">
          <div className="flex items-center gap-1.5 text-xs text-gray-400">
            <HelpCircle className="w-3.5 h-3.5 text-gray-500" />
            <span>Safety Test: Attempt out-of-geofence command to verify backend rejection.</span>
          </div>
          <button
            onClick={handleSummon20kmBad}
            disabled={isSending}
            className="text-xs text-gray-400 hover:text-tactical-warn underline transition-colors cursor-pointer"
          >
            Test: Summon 20 km away (Safety Reject Test)
          </button>
        </div>
      </div>

      {/* 3. RECENT COMMANDS REALTIME FEED */}
      <div className="bg-bg-card border border-bg-line rounded-2xl p-5 shadow-xl space-y-4">
        <div className="flex items-center justify-between pb-3 border-b border-bg-line">
          <div className="flex items-center gap-2 text-white font-bold text-sm">
            <Clock className="w-4 h-4 text-tactical-cyan" />
            <span>LIVE COMMANDS LOG (REALTIME)</span>
          </div>
          <span className="text-xs font-mono text-gray-400">
            {commands.length} Commands Tracked
          </span>
        </div>

        {commands.length === 0 ? (
          <div className="text-center py-8 text-xs text-gray-500 font-mono">
            No commands issued yet in this session.
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead>
                <tr className="text-gray-400 border-b border-bg-line font-mono uppercase text-[11px]">
                  <th className="py-2.5 pr-4">Type</th>
                  <th className="py-2.5 px-3">Status</th>
                  <th className="py-2.5 px-3">Target / Reason</th>
                  <th className="py-2.5 px-3">Issued By</th>
                  <th className="py-2.5 pl-3">Issued Time</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-bg-line font-mono">
                {commands.map((cmd) => {
                  const statusVariant =
                    cmd.status === 'done' || cmd.status === 'accepted' || cmd.status === 'executing'
                      ? 'ok'
                      : cmd.status === 'rejected'
                      ? 'bad'
                      : cmd.status === 'pending'
                      ? 'warn'
                      : 'neutral';

                  const isSos = cmd.type.toLowerCase() === 'sos';

                  return (
                    <tr
                      key={cmd.id}
                      className={`hover:bg-bg-line/20 transition-colors ${
                        isSos ? 'bg-tactical-bad/5' : ''
                      }`}
                    >
                      <td className="py-3 pr-4 font-bold text-white uppercase">
                        {isSos ? (
                          <span className="text-tactical-bad font-black flex items-center gap-1">
                            <ShieldAlert className="w-3.5 h-3.5 inline animate-pulse text-tactical-bad" />
                            SOS
                          </span>
                        ) : (
                          cmd.type
                        )}
                      </td>
                      <td className="py-3 px-3">
                        <Badge variant={statusVariant} size="sm">
                          {cmd.status}
                        </Badge>
                      </td>
                      <td className="py-3 px-3 text-gray-300 max-w-xs truncate">
                        {cmd.rejected_reason ? (
                          <span className="text-tactical-bad font-semibold">
                            ✗ {cmd.rejected_reason}
                          </span>
                        ) : cmd.target_lat != null && cmd.target_lon != null ? (
                          <span>
                            📍 {cmd.target_lat.toFixed(4)}, {cmd.target_lon.toFixed(4)}
                          </span>
                        ) : (
                          <span className="text-gray-500">—</span>
                        )}
                      </td>
                      <td className="py-3 px-3 text-gray-400 truncate max-w-[120px]">
                        {cmd.issued_by || 'operator'}
                      </td>
                      <td className="py-3 pl-3 text-gray-400 whitespace-nowrap">
                        {formatDateTime(cmd.issued_at)} ({timeAgo(cmd.issued_at)})
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
};
