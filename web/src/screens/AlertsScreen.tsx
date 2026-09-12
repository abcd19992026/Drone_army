import React from 'react';
import { BellRing, ShieldAlert, MessageSquare, Phone, Clock } from 'lucide-react';
import { Badge } from '../components/Badge';
import { formatFullDateTime, timeAgo } from '../lib/utils';
import type { AlertRow } from '../types';

interface AlertsScreenProps {
  alerts: AlertRow[];
  loading: boolean;
}

export const AlertsScreen: React.FC<AlertsScreenProps> = ({ alerts, loading }) => {
  return (
    <div className="space-y-6 max-w-7xl mx-auto pb-16">
      <div className="bg-bg-card border border-bg-line rounded-2xl p-5 shadow-xl space-y-4">
        <div className="flex items-center justify-between pb-3 border-b border-bg-line">
          <div>
            <h2 className="text-base sm:text-lg font-bold text-white flex items-center gap-2">
              <BellRing className="w-5 h-5 text-tactical-warn" />
              <span>SAFETY & WEATHER ALERTS</span>
            </h2>
            <p className="text-xs text-gray-400 mt-0.5">
              Automated contact notifications broadcasted on weather holds, safety diverts, and emergency triggers.
            </p>
          </div>
          <span className="text-xs font-mono text-gray-400">
            {alerts.length} Alerts
          </span>
        </div>

        {loading ? (
          <div className="text-center py-12 text-gray-400 font-mono text-xs flex flex-col items-center gap-2">
            <div className="w-6 h-6 border-2 border-tactical-cyan border-t-transparent rounded-full animate-spin" />
            <span>Loading safety alerts...</span>
          </div>
        ) : alerts.length === 0 ? (
          <div className="text-center py-12 text-gray-500 font-mono text-xs">
            No safety diverts or emergency alerts triggered.
          </div>
        ) : (
          <div className="space-y-3">
            {alerts.map((alert) => {
              const statusVariant =
                alert.status === 'sent'
                  ? 'ok'
                  : alert.status === 'failed'
                  ? 'bad'
                  : 'warn';

              return (
                <div
                  key={alert.id}
                  className="p-4 rounded-xl bg-bg-input border border-bg-line hover:border-tactical-warn/40 transition-colors space-y-2.5"
                >
                  <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2">
                    <div className="flex items-center gap-2">
                      <div className="p-1.5 rounded-lg bg-tactical-warn/15 border border-tactical-warn/30 text-tactical-warn">
                        {alert.channel === 'whatsapp' ? (
                          <MessageSquare className="w-4 h-4" />
                        ) : alert.channel === 'voice' ? (
                          <Phone className="w-4 h-4" />
                        ) : (
                          <ShieldAlert className="w-4 h-4" />
                        )}
                      </div>
                      <span className="font-bold text-sm text-white uppercase font-mono">
                        {alert.channel} Notification
                      </span>
                      <Badge variant={statusVariant} size="sm">
                        {alert.status}
                      </Badge>
                    </div>

                    <div className="text-xs text-gray-400 font-mono flex items-center gap-1.5">
                      <Clock className="w-3.5 h-3.5" />
                      <span>{formatFullDateTime(alert.triggered_at)}</span>
                      <span>({timeAgo(alert.triggered_at)})</span>
                    </div>
                  </div>

                  {/* Plain Language Summary / Alert Detail */}
                  <div className="text-xs text-gray-300">
                    {alert.template_name && (
                      <div className="font-mono text-gray-400 mb-1">
                        Template: <span className="text-tactical-cyan">{alert.template_name}</span>
                      </div>
                    )}

                    {alert.detail && (
                      <div className="p-2.5 rounded-lg bg-bg-card border border-bg-line font-mono text-xs text-tactical-warn whitespace-pre-wrap">
                        {typeof alert.detail === 'string'
                          ? alert.detail
                          : JSON.stringify(alert.detail, null, 2)}
                      </div>
                    )}

                    {alert.error_detail && (
                      <div className="text-tactical-bad font-mono text-xs mt-1">
                        Error: {alert.error_detail}
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
  );
};
