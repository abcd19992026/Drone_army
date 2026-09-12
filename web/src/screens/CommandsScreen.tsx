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

  // 1. Summon Handlers
  const handleSummonHere = () => {
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
        await onIssueCommand('summon', {
          target_lat: latitude,
          target_lon: longitude,
        });
      },
      (err) => {
        setGeoLocating(false);
        let msg = 'Could not acquire GPS position.';
        if (err.code === err.PERMISSION_DENIED) {
          msg = 'Location permission denied. Please enable GPS location to summon.';
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

  const handleSummon20kmBad = async () => {
    // Deliberate test: ~20 km north of dock to verify backend safety geofence rejection
    const badLat = APP_CONFIG.HOME_LAT + 0.18;
    const badLon = APP_CONFIG.HOME_LON;
    await onIssueCommand('summon', {
      target_lat: badLat,
      target_lon: badLon,
    });
  };

  // 2. Weather Hold Handlers
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
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-3.5">
          {/* Summon (Here) */}
          <button
            onClick={handleSummonHere}
            disabled={isSending || geoLocating}
            className="flex flex-col items-center justify-center p-4 rounded-xl bg-tactical-blue hover:bg-tactical-blue/90 text-white font-bold transition-all disabled:opacity-50 shadow-lg shadow-tactical-blue/20 active:scale-98 group"
          >
            <div className="w-10 h-10 rounded-full bg-white/10 flex items-center justify-center mb-2 group-hover:scale-110 transition-transform">
              <MapPin className="w-5 h-5 text-white" />
            </div>
            <span className="text-sm uppercase tracking-wider">
              {geoLocating ? 'Acquiring GPS...' : 'Summon (Here)'}
            </span>
            <span className="text-[10px] font-normal text-blue-200 mt-0.5">
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
            <span className="text-sm uppercase tracking-wider">Hold In Place</span>
            <span className="text-[10px] font-normal text-gray-400 mt-0.5">
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
            <span className="text-sm uppercase tracking-wider">Abort & Return</span>
            <span className="text-[10px] font-normal text-gray-300 mt-0.5">
              Failsafe RTL back to dock
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
            className="text-xs text-gray-400 hover:text-tactical-warn underline transition-colors"
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

                  return (
                    <tr key={cmd.id} className="hover:bg-bg-line/20 transition-colors">
                      <td className="py-3 pr-4 font-bold text-white uppercase">
                        {cmd.type}
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
