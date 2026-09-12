import React, { useState } from 'react';
import { useAuth } from './context/AuthContext';
import { Header } from './components/Header';
import { Navigation } from './components/Navigation';
import { LoginScreen } from './screens/LoginScreen';
import { LiveStatusScreen } from './screens/LiveStatusScreen';
import { MapScreen } from './screens/MapScreen';
import { CommandsScreen } from './screens/CommandsScreen';
import { MissionHistoryScreen } from './screens/MissionHistoryScreen';
import { AlertsScreen } from './screens/AlertsScreen';
import { useDroneRealtime } from './hooks/useDroneRealtime';
import { useCommands } from './hooks/useCommands';
import { useMissions } from './hooks/useMissions';
import { useAlerts } from './hooks/useAlerts';
import type { ActiveScreen } from './types';
import { Radio } from 'lucide-react';

export const App: React.FC = () => {
  const { user, loading: authLoading } = useAuth();
  const [activeScreen, setActiveScreen] = useState<ActiveScreen>('status');

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

  const { alerts, loading: alertsLoading } = useAlerts();

  // 1. Loading state for initial session check
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

  // 2. Auth Guard: Not logged in
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

        {activeScreen === 'alerts' && (
          <AlertsScreen alerts={alerts} loading={alertsLoading} />
        )}
      </main>
    </div>
  );
};
