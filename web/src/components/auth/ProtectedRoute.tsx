"use client";

import { Navigate, Outlet } from "react-router-dom";
import { useAuth } from "@/context/AuthContext";

export function ProtectedRoute() {
  const { session, loading } = useAuth();

  if (loading) {
    return (
      <div className="app app--loading" role="status" aria-label="Loading authentication state">
        <div className="app__loading-spinner" />
      </div>
    );
  }

  if (!session?.authenticated) {
    return <Navigate to="/auth" replace />;
  }

  return <Outlet />;
}

export function PublicRoute() {
  const { session, loading } = useAuth();

  if (loading) {
    return (
      <div className="app app--loading" role="status" aria-label="Loading authentication state">
        <div className="app__loading-spinner" />
      </div>
    );
  }

  if (session?.authenticated) {
    return <Navigate to="/app" replace />;
  }

  return <Outlet />;
}