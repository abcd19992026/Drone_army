import React from 'react';
import { Activity, Map as MapIcon, Send, History, CalendarClock, BellRing } from 'lucide-react';
import type { ActiveScreen } from '../types';

interface NavigationProps {
  activeScreen: ActiveScreen;
  onSelectScreen: (screen: ActiveScreen) => void;
  hasWeatherHold: boolean;
  unreadAlertsCount?: number;
}

export const Navigation: React.FC<NavigationProps> = ({
  activeScreen,
  onSelectScreen,
  hasWeatherHold,
  unreadAlertsCount = 0,
}) => {
  const navItems: {
    id: ActiveScreen;
    label: string;
    icon: React.ReactNode;
    badge?: React.ReactNode;
  }[] = [
    {
      id: 'status',
      label: 'Status',
      icon: <Activity className="w-5 h-5" />,
    },
    {
      id: 'map',
      label: 'Map Radar',
      icon: <MapIcon className="w-5 h-5" />,
    },
    {
      id: 'commands',
      label: 'Commands',
      icon: <Send className="w-5 h-5" />,
      badge: hasWeatherHold ? (
        <span className="w-2.5 h-2.5 rounded-full bg-tactical-warn animate-ping" />
      ) : undefined,
    },
    {
      id: 'missions',
      label: 'Missions',
      icon: <History className="w-5 h-5" />,
    },
    {
      id: 'schedules',
      label: 'Schedules',
      icon: <CalendarClock className="w-5 h-5" />,
    },
    {
      id: 'alerts',
      label: 'Alerts',
      icon: <BellRing className="w-5 h-5" />,
      badge: unreadAlertsCount > 0 ? (
        <span className="px-1.5 py-0.2 text-[10px] font-bold rounded-full bg-tactical-bad text-white">
          {unreadAlertsCount}
        </span>
      ) : undefined,
    },
  ];

  return (
    <>
      {/* Desktop / Tablet Top Nav Tab Bar */}
      <nav className="hidden md:flex items-center gap-1 bg-bg-card border-b border-bg-line px-6 py-2">
        <div className="max-w-7xl mx-auto w-full flex items-center justify-between">
          <div className="flex items-center gap-2">
            {navItems.map((item) => {
              const isActive = activeScreen === item.id;
              return (
                <button
                  key={item.id}
                  onClick={() => onSelectScreen(item.id)}
                  className={`flex items-center gap-2 px-4 py-2 rounded-lg text-xs font-bold uppercase tracking-wider transition-all relative ${
                    isActive
                      ? 'bg-tactical-cyan/15 text-tactical-cyan border border-tactical-cyan/30 shadow-[0_0_12px_rgba(0,229,255,0.15)]'
                      : 'text-gray-400 hover:text-gray-200 hover:bg-bg-line/50 border border-transparent'
                  }`}
                >
                  {item.icon}
                  <span>{item.label}</span>
                  {item.badge && <span className="ml-1">{item.badge}</span>}
                </button>
              );
            })}
          </div>

          <div className="text-[11px] text-gray-500 font-mono">
            Autonomous Companion • v0.7 PWA
          </div>
        </div>
      </nav>

      {/* Mobile Bottom Fixed Navigation Bar */}
      <nav className="md:hidden fixed bottom-0 left-0 right-0 z-50 bg-bg-card/95 backdrop-blur-lg border-t border-bg-line px-2 py-1.5 pb-safe safe-bottom">
        <div className="flex items-center justify-around">
          {navItems.map((item) => {
            const isActive = activeScreen === item.id;
            return (
              <button
                key={item.id}
                onClick={() => onSelectScreen(item.id)}
                className={`flex flex-col items-center justify-center py-1 px-3 rounded-lg text-[10px] font-medium tracking-wide transition-all relative ${
                  isActive
                    ? 'text-tactical-cyan font-bold scale-105'
                    : 'text-gray-400 hover:text-gray-200'
                }`}
              >
                <div className="relative">
                  {item.icon}
                  {item.badge && (
                    <div className="absolute -top-1 -right-2">
                      {item.badge}
                    </div>
                  )}
                </div>
                <span className="mt-1">{item.label}</span>
                {isActive && (
                  <span className="absolute bottom-0 w-8 h-0.5 rounded-full bg-tactical-cyan" />
                )}
              </button>
            );
          })}
        </div>
      </nav>
    </>
  );
};
