"use client";

import { BrowserRouter, Routes, Route } from "react-router-dom";
import { AuthProviderComponent } from "@/context/AuthContext";
import { LandingPage } from "@/pages/LandingPage";
import { AuthPage } from "@/pages/AuthPage";
import { AuthCallbackPage } from "@/pages/AuthCallbackPage";
import { AppPage } from "@/pages/AppPage";
import { ProtectedRoute, PublicRoute } from "@/components/auth/ProtectedRoute";

export function App() {
  return (
    <BrowserRouter>
      <AuthProviderComponent>
        <Routes>
          <Route path="/" element={<LandingPage />} />
          <Route element={<PublicRoute />}>
            <Route path="/auth" element={<AuthPage />} />
            <Route path="/auth/callback" element={<AuthCallbackPage />} />
          </Route>
          <Route element={<ProtectedRoute />}>
            <Route path="/app" element={<AppPage />} />
          </Route>
        </Routes>
      </AuthProviderComponent>
    </BrowserRouter>
  );
}