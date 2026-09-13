import React from 'react';
import { cn } from '../lib/utils';

export interface BadgeProps extends React.HTMLAttributes<HTMLSpanElement> {
  variant?: 'ok' | 'warn' | 'bad' | 'cyan' | 'blue' | 'purple' | 'neutral' | 'outline';
  size?: 'sm' | 'md' | 'lg';
}

export const Badge: React.FC<BadgeProps> = ({
  children,
  variant = 'neutral',
  size = 'md',
  className,
  ...props
}) => {
  const variantStyles = {
    ok: 'bg-tactical-ok/15 text-tactical-ok border-tactical-ok/30',
    warn: 'bg-tactical-warn/15 text-tactical-warn border-tactical-warn/30',
    bad: 'bg-tactical-bad/15 text-tactical-bad border-tactical-bad/30',
    cyan: 'bg-tactical-cyan/15 text-tactical-cyan border-tactical-cyan/30',
    blue: 'bg-tactical-blue/15 text-tactical-blue border-tactical-blue/30',
    purple: 'bg-tactical-purple/15 text-tactical-purple border-tactical-purple/30',
    neutral: 'bg-gray-800 text-gray-300 border-gray-700',
    outline: 'bg-transparent text-gray-300 border-gray-700',
  };

  const sizeStyles = {
    sm: 'text-[10px] px-2 py-0.5 tracking-wider',
    md: 'text-xs px-2.5 py-1 tracking-wide',
    lg: 'text-sm px-3.5 py-1.5 font-bold tracking-wide',
  };

  return (
    <span
      className={cn(
        'inline-flex items-center gap-1.5 font-semibold uppercase rounded-full border shadow-sm transition-colors',
        variantStyles[variant],
        sizeStyles[size],
        className
      )}
      {...props}
    >
      {children}
    </span>
  );
};
