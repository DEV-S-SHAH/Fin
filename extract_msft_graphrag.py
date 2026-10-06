#!/usr/bin/env python3
"""Extract MSFT entities and relationships using graphrag extraction pipeline.

Reads MSFT chunks from sandbox_engine staging, runs graphrag extraction,
and outputs to data/staging/MSFT/{chunks,entities,relationships}/
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pyarrow.parquet as pq

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from graphrag.config import GraphRAGConfig
from graphrag.extract import extract_chunk
from graphrag.llm import resolve_client, HeuristicClient
from graphrag.store import GraphStore


def load_msft_chunks(staging_dir: Path) -> list[dict]:
    """Load all MSFT text chunks from sandbox_engine staging."""
    # Get MSFT filing IDs
    submitted_table = pq.read_table(staging_dir / "SUBMITTED.part0000.parquet")
    msft_filing_ids = set()
    for i in range(submitted_table.num_rows):
        row = dict(zip(submitted_table.column_names, 
                       [submitted_table.column(j)[i].as_py() for j in range(len(submitted_table.column_names))]))
        if row.get('from') == 'MSFT':
            msft_filing_ids.add(row.get('to'))
    
    print(f"Found {len(msft_filing_ids)} MSFT filings")
    
    # Get MSFT chunk IDs via HAS_CHUNK
    has_chunk_table = pq.read_table(staging_dir / "HAS_CHUNK.part0000.parquet")
    msft_chunk_ids = set()
    for i in range(has_chunk_table.num_rows):
        row = dict(zip(has_chunk_table.column_names, 
                       [has_chunk_table.column(j)[i].as_py() for j in range(len(has_chunk_table.column_names))]))
        if row.get('from') in msft_filing_ids:
            msft_chunk_ids.add(row.get('to'))
    
    print(f"Found {len(msft_chunk_ids)} MSFT chunks")
    
    # Load chunk texts
    chunk_table = pq.read_table(staging_dir / "Chunk.part0000.parquet")
    chunks = []
    for i in range(chunk_table.num_rows):
        row = dict(zip(chunk_table.column_names, 
                       [chunk_table.column(j)[i].as_py() for j in range(len(chunk_table.column_names))]))
        if row.get('id') in msft_chunk_ids:
            chunks.append({
                'id': row.get('id'),
                'section': row.get('section', ''),
                'text': row.get('text', '')
            })
    
    print(f"Loaded {len(chunks)} MSFT chunk texts")
    return chunks


def extract_entities_relationships(chunks: list[dict], config: GraphRAGConfig) -> tuple[list[dict], list[dict]]:
    """Run graphrag extraction on chunks using HeuristicClient."""
    client = HeuristicClient()
    all_entities = []
    all_relationships = []
    
    for i, chunk in enumerate(chunks):
        if i % 100 == 0:
            print(f"Processing chunk {i+1}/{len(chunks)}")
        
        text = chunk.get('text', '')
        if not text or len(text.strip()) < 50:
            continue
        
        location = f"MSFT chunk {chunk['id']} section {chunk['section']}"
        extraction = extract_chunk(client, text, config, location=location)
        
        if extraction.error:
            print(f"  Chunk {chunk['id']}: extraction error: {extraction.error}")
            continue
        
        for entity in extraction.entities:
            all_entities.append({
                'id': entity.id,
                'name': entity.name,
                'entity_type': entity.entity_type,
                'description': entity.description,
                'source_chunk': chunk['id'],
                'source_section': chunk['section'],
            })
        
        for rel in extraction.relationships:
            all_relationships.append({
                'source_id': rel.source_id,
                'target_id': rel.target_id,
                'source': rel.source,
                'target': rel.target,
                'relation': rel.relation,
                'description': rel.description,
                'source_chunk': chunk['id'],
                'source_section': chunk['section'],
            })
    
    return all_entities, all_relationships


def deduplicate_entities(entities: list[dict]) -> list[dict]:
    """Deduplicate entities by ID, merging descriptions."""
    by_id = {}
    for entity in entities:
        eid = entity['id']
        if eid not in by_id:
            by_id[eid] = entity
        else:
            # Merge descriptions
            existing = by_id[eid]
            if entity['description'] and entity['description'] not in existing['description']:
                existing['description'] = existing['description'] + " | " + entity['description']
            # Use more specific entity_type if available
            if entity['entity_type'] != 'term' and existing['entity_type'] == 'term':
                existing['entity_type'] = entity['entity_type']
    return list(by_id.values())


def deduplicate_relationships(relationships: list[dict]) -> list[dict]:
    """Deduplicate relationships by (source_id, target_id, relation)."""
    seen = set()
    deduped = []
    for rel in relationships:
        key = (rel['source_id'], rel['target_id'], rel['relation'])
        if key not in seen:
            seen.add(key)
            deduped.append(rel)
    return deduped


def main():
    staging_dir = Path("sandbox_engine/_run/staging")
    output_dir = Path("data/staging/MSFT")
    
    # Create output directories
    (output_dir / "chunks").mkdir(parents=True, exist_ok=True)
    (output_dir / "entities").mkdir(parents=True, exist_ok=True)
    (output_dir / "relationships").mkdir(parents=True, exist_ok=True)
    
    print("Loading MSFT chunks from sandbox_engine staging...")
    chunks = load_msft_chunks(staging_dir)
    
    if not chunks:
        print("No MSFT chunks found!")
        return
    
    # Save chunks for reference
    chunks_file = output_dir / "chunks" / "msft_chunks.jsonl"
    with open(chunks_file, 'w') as f:
        for chunk in chunks:
            f.write(json.dumps(chunk) + '\n')
    print(f"Saved {len(chunks)} chunks to {chunks_file}")
    
    # Configure graphrag
    config = GraphRAGConfig(
        chunk_tokens=800,
        chunk_overlap_tokens=100,
        max_entities_per_chunk=40,
        max_relationships_per_chunk=60,
        min_entity_name_length=2,
        min_description_length=8,
    )
    
    print("Running graphrag extraction (heuristic mode)...")
    entities, relationships = extract_entities_relationships(chunks, config)
    
    print(f"Raw extraction: {len(entities)} entities, {len(relationships)} relationships")
    
    # Deduplicate
    entities = deduplicate_entities(entities)
    relationships = deduplicate_relationships(relationships)
    
    print(f"After deduplication: {len(entities)} entities, {len(relationships)} relationships")
    
    # Save entities
    entities_file = output_dir / "entities" / "msft_entities.jsonl"
    with open(entities_file, 'w') as f:
        for entity in entities:
            f.write(json.dumps(entity) + '\n')
    print(f"Saved {len(entities)} entities to {entities_file}")
    
    # Save relationships
    relationships_file = output_dir / "relationships" / "msft_relationships.jsonl"
    with open(relationships_file, 'w') as f:
        for rel in relationships:
            f.write(json.dumps(rel) + '\n')
    print(f"Saved {len(relationships)} relationships to {relationships_file}")
    
    # Generate summary
    entity_types = {}
    for e in entities:
        entity_types[e['entity_type']] = entity_types.get(e['entity_type'], 0) + 1
    
    relation_types = {}
    for r in relationships:
        relation_types[r['relation']] = relation_types.get(r['relation'], 0) + 1
    
    summary = {
        'company': 'MSFT',
        'cik': '789019',
        'total_chunks_processed': len(chunks),
        'total_entities': len(entities),
        'total_relationships': len(relationships),
        'entity_types': entity_types,
        'relation_types': relation_types,
        'staging_paths': {
            'chunks': str(chunks_file),
            'entities': str(entities_file),
            'relationships': str(relationships_file),
        }
    }
    
    summary_file = output_dir / "msft_extraction_summary.json"
    with open(summary_file, 'w') as f:
        json.dump(summary, f, indent=2)
    print(f"Saved summary to {summary_file}")
    
    print("\n=== EXTRACTION SUMMARY ===")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()