import React, { useState, useEffect } from 'react';
import { useAuth } from './context/AuthContext';
import { Header } from './components/Header';
import { Navigation } from './components/Navigation';
import { LoginScreen } from './screens/LoginScreen';
import { LiveStatusScreen } from './screens/LiveStatusScreen';
import { MapScreen } from './screens/MapScreen';
import { CommandsScreen } from './screens/CommandsScreen';
import { MissionHistoryScreen } from './screens/MissionHistoryScreen';
import { PatrolSchedulesScreen } from './screens/PatrolSchedulesScreen';
import { AlertsScreen } from './screens/AlertsScreen';
import { PublicSosStatusScreen } from './screens/PublicSosStatusScreen';
import { useDroneRealtime } from './hooks/useDroneRealtime';
import { useCommands } from './hooks/useCommands';
import { useMissions } from './hooks/useMissions';
import { usePatrolSchedules } from './hooks/usePatrolSchedules';
import { useAlerts } from './hooks/useAlerts';
import type { ActiveScreen } from './types';
import { Radio } from 'lucide-react';

function getSosCommandIdFromUrl(): string | null {
  if (typeof window === 'undefined') return null;

  // 1. Path: /status/:id or /status/:id/
  const pathname = window.location.pathname;
  const statusMatch = pathname.match(/^\/status\/([a-zA-Z0-9_-]+)/i);
  if (statusMatch && statusMatch[1]) {
    return statusMatch[1];
  }

  // 2. Direct UUID path: /<uuid>
  const uuidMatch = pathname.match(/^\/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$/i);
  if (uuidMatch && uuidMatch[1]) {
    return uuidMatch[1];
  }

  // 3. Hash: #/status/:id or #/status/<uuid>
  const hash = window.location.hash;
  const hashMatch = hash.match(/^#\/?status\/([a-zA-Z0-9_-]+)/i);
  if (hashMatch && hashMatch[1]) {
    return hashMatch[1];
  }

  // 4. Query param: ?status=:id or ?command_id=:id or ?commandId=:id
  const searchParams = new URLSearchParams(window.location.search);
  const paramId = searchParams.get('command_id') || searchParams.get('commandId') || searchParams.get('status');
  if (paramId) {
    return paramId;
  }

  return null;
}

export const App: React.FC = () => {
  const [publicSosCommandId, setPublicSosCommandId] = useState<string | null>(() => getSosCommandIdFromUrl());

  useEffect(() => {
    const handleUrlChange = () => {
      setPublicSosCommandId(getSosCommandIdFromUrl());
    };
    window.addEventListener('popstate', handleUrlChange);
    window.addEventListener('hashchange', handleUrlChange);
    return () => {
      window.removeEventListener('popstate', handleUrlChange);
      window.removeEventListener('hashchange', handleUrlChange);
    };
  }, []);

  const { user, loading: authLoading } = useAuth();
  const [activeScreen, setActiveScreen] = useState<ActiveScreen>('status');

  // 1. Public SOS Status Route (NO LOGIN REQUIRED)
  if (publicSosCommandId) {
    return <PublicSosStatusScreen commandId={publicSosCommandId} />;
  }

  // Ground Station Hooks
  const { drone, dock, isStale, staleSeconds, isLinkDown } = useDroneRealtime();
  const {
    commands,
    isSending,
    statusMessage,
    issueCommand,
  } = useCommands(user?.email);

  const {
    missions,
    activeMission,
    weatherHoldMission,
    selectedMissionEvents,
    loadingEvents,
    fetchMissionEvents,
  } = useMissions();

  const {
    schedules,
    loading: schedulesLoading,
    actionLoading: schedulesActionLoading,
    statusMessage: schedulesStatusMessage,
    createSchedule,
    updateSchedule,
    toggleScheduleActive,
    deleteSchedule,
  } = usePatrolSchedules();

  const { alerts, loading: alertsLoading } = useAlerts();

  // 2. Loading state for initial session check
  if (authLoading) {
    return (
      <div className="min-h-screen bg-bg flex flex-col items-center justify-center text-tactical-cyan">
        <div className="relative mb-4">
          <div className="w-16 h-16 rounded-full border-2 border-tactical-cyan border-t-transparent animate-spin" />
          <Radio className="w-6 h-6 absolute inset-0 m-auto animate-pulse" />
        </div>
        <p className="text-xs uppercase tracking-widest font-mono text-gray-400">
          Connecting to GSS Supabase Realtime...
        </p>
      </div>
    );
  }

  // 3. Auth Guard: Not logged in
  if (!user) {
    return <LoginScreen />;
  }

  // 3. Authenticated Operator Ground Station
  return (
    <div className="min-h-screen bg-bg text-gray-100 flex flex-col selection:bg-tactical-cyan/20 selection:text-tactical-cyan">
      {/* Top Header */}
      <Header
        drone={drone}
        isStale={isStale}
        staleSeconds={staleSeconds}
        isLinkDown={isLinkDown}
      />

      {/* Navigation (Desktop Top Bar + Mobile Bottom Bar) */}
      <Navigation
        activeScreen={activeScreen}
        onSelectScreen={setActiveScreen}
        hasWeatherHold={weatherHoldMission !== null}
        unreadAlertsCount={alerts.length}
      />

      {/* Main Content Area */}
      <main className="flex-1 px-4 sm:px-6 py-5 max-w-7xl mx-auto w-full">
        {activeScreen === 'status' && (
          <LiveStatusScreen
            drone={drone}
            dock={dock}
            isStale={isStale}
            staleSeconds={staleSeconds}
            isLinkDown={isLinkDown}
            onNavigateToMap={() => setActiveScreen('map')}
          />
        )}

        {activeScreen === 'map' && (
          <MapScreen
            drone={drone}
            dock={dock}
            activeMission={activeMission}
            isStale={isStale}
            onIssueCommand={issueCommand}
            isSending={isSending}
          />
        )}

        {activeScreen === 'commands' && (
          <CommandsScreen
            weatherHoldMission={weatherHoldMission}
            commands={commands}
            isSending={isSending}
            statusMessage={statusMessage}
            onIssueCommand={issueCommand}
          />
        )}

        {activeScreen === 'missions' && (
          <MissionHistoryScreen
            missions={missions}
            selectedMissionEvents={selectedMissionEvents}
            loadingEvents={loadingEvents}
            onSelectMission={fetchMissionEvents}
          />
        )}

        {activeScreen === 'schedules' && (
          <PatrolSchedulesScreen
            schedules={schedules}
            loading={schedulesLoading}
            actionLoading={schedulesActionLoading}
            statusMessage={schedulesStatusMessage}
            onCreateSchedule={createSchedule}
            onUpdateSchedule={updateSchedule}
            onToggleActive={toggleScheduleActive}
            onDeleteSchedule={deleteSchedule}
          />
        )}

        {activeScreen === 'alerts' && (
          <AlertsScreen alerts={alerts} loading={alertsLoading} />
        )}
      </main>
    </div>
  );
};
