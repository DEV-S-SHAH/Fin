/**
 * Animation Page Entry Point
 * 
 * Mounts the GraphRAGRunPage component for the /animation route.
 */

import { render } from 'preact';
import { GraphRAGRunPage } from './pages/GraphRAGRunPage.js';

const root = document.getElementById('animation-root');
if (root) {
  render(<GraphRAGRunPage />, root);
}