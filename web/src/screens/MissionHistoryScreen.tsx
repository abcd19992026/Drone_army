import React, { useState } from 'react';
import {
  History,
  ChevronRight,
  ShieldAlert,
  CloudRain,
  Battery,
  Navigation,
  X,
  CheckCircle,
} from 'lucide-react';
import { Badge } from '../components/Badge';
import { formatNumber, formatDateTime } from '../lib/utils';
import type { MissionRow, MissionEventRow } from '../types';

interface MissionHistoryScreenProps {
  missions: MissionRow[];
  selectedMissionEvents: MissionEventRow[];
  loadingEvents: boolean;
  onSelectMission: (missionId: string) => void;
}

export const MissionHistoryScreen: React.FC<MissionHistoryScreenProps> = ({
  missions,
  selectedMissionEvents,
  loadingEvents,
  onSelectMission,
}) => {
  const [activeMissionId, setActiveMissionId] = useState<string | null>(null);

  const handleOpenTimeline = (mission: MissionRow) => {
    setActiveMissionId(mission.id);
    onSelectMission(mission.id);
  };

  const activeMission = missions.find((m) => m.id === activeMissionId);

  return (
    <div className="space-y-6 max-w-7xl mx-auto pb-16">
      <div className="bg-bg-card border border-bg-line rounded-2xl p-5 shadow-xl space-y-4">
        <div className="flex items-center justify-between pb-3 border-b border-bg-line">
          <div>
            <h2 className="text-base sm:text-lg font-bold text-white flex items-center gap-2">
              <History className="w-5 h-5 text-tactical-cyan" />
              <span>MISSION FLIGHT LOGS</span>
            </h2>
            <p className="text-xs text-gray-400 mt-0.5">
              Historical flight records. Tap any mission to inspect the full timeline story and safety events.
            </p>
          </div>
          <span className="text-xs font-mono text-gray-400">
            {missions.length} Missions Logged
          </span>
        </div>

        {missions.length === 0 ? (
          <div className="text-center py-12 text-gray-500 font-mono text-xs">
            No flight missions recorded in database yet.
          </div>
        ) : (
          <div className="divide-y divide-bg-line">
            {missions.map((m) => {
              const statusVariant =
                m.status === 'landed'
                  ? 'ok'
                  : m.status === 'aborted'
                  ? 'bad'
                  : m.status === 'awaiting_confirmation'
                  ? 'warn'
                  : 'cyan';

              return (
                <div
                  key={m.id}
                  onClick={() => handleOpenTimeline(m)}
                  className="py-4 px-2 sm:px-4 rounded-xl hover:bg-bg-line/30 cursor-pointer transition-all flex flex-col sm:flex-row items-start sm:items-center justify-between gap-3 group"
                >
                  <div className="flex items-start gap-3">
                    <div className="w-9 h-9 rounded-xl bg-bg-input border border-bg-line flex items-center justify-center shrink-0 group-hover:border-tactical-cyan/40 transition-colors">
                      <Navigation className="w-4 h-4 text-tactical-cyan" />
                    </div>
                    <div>
                      <div className="flex items-center gap-2">
                        <span className="font-bold text-sm text-white uppercase tracking-wider">
                          {m.type}
                        </span>
                        <Badge variant={statusVariant} size="sm">
                          {m.status}
                        </Badge>
                      </div>
                      <div className="text-xs text-gray-400 font-mono mt-1 flex flex-wrap items-center gap-x-3 gap-y-1">
                        <span>ID: #{m.id.slice(0, 8)}</span>
                        <span>•</span>
                        <span>Started: {formatDateTime(m.started_at || m.created_at)}</span>
                        {m.distance_m != null && (
                          <>
                            <span>•</span>
                            <span>Dist: {formatNumber(m.distance_m, 0, ' m')}</span>
                          </>
                        )}
                        {m.battery_used_pct != null && (
                          <>
                            <span>•</span>
                            <span>Bat Used: {formatNumber(m.battery_used_pct, 0, '%')}</span>
                          </>
                        )}
                      </div>
                    </div>
                  </div>

                  <div className="flex items-center gap-3 self-end sm:self-center">
                    <span className="text-xs text-tactical-cyan font-semibold group-hover:underline flex items-center gap-1">
                      <span>Inspect Timeline</span>
                      <ChevronRight className="w-4 h-4" />
                    </span>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>

      {/* 2. MISSION EVENTS TIMELINE DRAWER / MODAL */}
      {activeMissionId && (
        <div className="fixed inset-0 z-50 bg-black/80 backdrop-blur-sm flex justify-end">
          <div className="w-full max-w-2xl bg-bg-card border-l border-bg-line h-full overflow-y-auto p-5 sm:p-6 shadow-2xl flex flex-col">
            {/* Header */}
            <div className="flex items-center justify-between pb-4 border-b border-bg-line">
              <div>
                <div className="flex items-center gap-2">
                  <h3 className="text-lg font-bold text-white uppercase tracking-wider">
                    {activeMission?.type} Mission Timeline
                  </h3>
                  <Badge
                    variant={
                      activeMission?.status === 'landed'
                        ? 'ok'
                        : activeMission?.status === 'aborted'
                        ? 'bad'
                        : 'warn'
                    }
                    size="sm"
                  >
                    {activeMission?.status}
                  </Badge>
                </div>
                <div className="text-xs text-gray-400 font-mono mt-1">
                  Mission ID: {activeMissionId}
                </div>
              </div>
              <button
                onClick={() => setActiveMissionId(null)}
                className="p-2 rounded-lg bg-bg-input hover:bg-bg-line text-gray-400 hover:text-white transition-colors"
              >
                <X className="w-5 h-5" />
              </button>
            </div>

            {/* Timeline Event List */}
            <div className="flex-1 py-6">
              {loadingEvents ? (
                <div className="text-center py-12 text-gray-400 font-mono text-xs flex flex-col items-center gap-2">
                  <div className="w-6 h-6 border-2 border-tactical-cyan border-t-transparent rounded-full animate-spin" />
                  <span>Loading flight event storyline...</span>
                </div>
              ) : selectedMissionEvents.length === 0 ? (
                <div className="text-center py-12 text-gray-500 font-mono text-xs">
                  No mission events recorded for this flight.
                </div>
              ) : (
                <div className="relative border-l-2 border-bg-line ml-4 space-y-6">
                  {selectedMissionEvents.map((event, idx) => {
                    const isSafety =
                      event.event === 'safety_veto' ||
                      event.event === 'geofence_block' ||
                      event.event === 'point_of_no_return' ||
                      event.event === 'battery_warning';

                    const isWeather =
                      event.event.startsWith('weather_');

                    const isSuccess =
                      event.event === 'landed' || event.event === 'arrived';

                    return (
                      <div key={event.id || idx} className="relative pl-6">
                        {/* Event icon dot */}
                        <div
                          className={`absolute -left-[17px] top-0 w-8 h-8 rounded-full border-2 flex items-center justify-center ${
                            isSafety
                              ? 'bg-tactical-bad/20 border-tactical-bad text-tactical-bad'
                              : isWeather
                              ? 'bg-tactical-warn/20 border-tactical-warn text-tactical-warn'
                              : isSuccess
                              ? 'bg-tactical-ok/20 border-tactical-ok text-tactical-ok'
                              : 'bg-bg-cardElevated border-tactical-cyan text-tactical-cyan'
                          }`}
                        >
                          {isSafety ? (
                            <ShieldAlert className="w-4 h-4" />
                          ) : isWeather ? (
                            <CloudRain className="w-4 h-4" />
                          ) : isSuccess ? (
                            <CheckCircle className="w-4 h-4" />
                          ) : (
                            <Navigation className="w-3.5 h-3.5" />
                          )}
                        </div>

                        {/* Event Content */}
                        <div className="bg-bg-input border border-bg-line rounded-xl p-3.5 shadow-sm space-y-2">
                          <div className="flex items-center justify-between">
                            <span className="font-bold text-sm text-white font-mono uppercase">
                              {event.event.replace(/_/g, ' ')}
                            </span>
                            <span className="text-[11px] font-mono text-gray-400">
                              {formatDateTime(event.at)}
                            </span>
                          </div>

                          {/* Telemetry Snapshot at this second */}
                          <div className="flex flex-wrap items-center gap-3 text-[11px] font-mono text-gray-400 pt-1 border-t border-bg-line/50">
                            {event.battery_pct != null && (
                              <span className="flex items-center gap-1 text-gray-300">
                                <Battery className="w-3 h-3 text-tactical-ok" />
                                {Math.round(event.battery_pct)}%
                              </span>
                            )}
                            {event.alt_m != null && (
                              <span>Alt: {formatNumber(event.alt_m, 1, ' m')}</span>
                            )}
                            {event.link_up !== null && (
                              <span
                                className={
                                  event.link_up
                                    ? 'text-tactical-ok'
                                    : 'text-tactical-warn'
                                }
                              >
                                Link: {event.link_up ? 'UP' : 'DOWN'}
                              </span>
                            )}
                            {event.lat != null && event.lon != null && (
                              <span>
                                Pos: {event.lat.toFixed(4)}, {event.lon.toFixed(4)}
                              </span>
                            )}
                          </div>

                          {/* Detail JSON / Reason explanation */}
                          {event.detail && Object.keys(event.detail).length > 0 && (
                            <div className="mt-2 p-2.5 rounded-lg bg-bg-card border border-bg-line text-xs font-mono text-gray-300">
                              <div className="text-[10px] text-gray-400 uppercase font-semibold mb-1">
                                Event Detail / Safety Reason:
                              </div>
                              <pre className="whitespace-pre-wrap text-[11px] text-tactical-warn">
                                {typeof event.detail === 'string'
                                  ? event.detail
                                  : JSON.stringify(event.detail, null, 2)}
                              </pre>
                            </div>
                          )}
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
};
