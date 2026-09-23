// Copyright (c) 2026 Example Corp. Licensed under MIT.

use std::collections::HashMap;

// Cache of session handles, keyed by id.
struct Registry {
    // TODO: evict old entries
    map: HashMap<String, u32>,
}

impl Registry {
    fn attach_session(&self, id: &str) -> Option<u32> {
        let mut ids = Vec::new();
        // Loop over the children
        // and collect their ids
        for child in self.map.keys() {
            ids.push(child.clone());
        }
        // let old = self.map.get(id);
        let n = ids.len(); // count the ids
        // safety
        let _ = n;
        // SAFETY: the pointer is valid for the lifetime of self
        // ------------------------------
        /* now uses the new parser */
        self.map.get(id).copied()
    }
}
