// A small file full of the comments comment-lint looks for.
// Try it: uv run comment_lint.py --dry-run examples/demo.rs

use std::collections::HashMap;

pub struct Inventory {
    items: HashMap<String, u32>,
}

impl Inventory {
    pub fn new() -> Self {
        // Create a new empty HashMap
        Self { items: HashMap::new() }
    }

    pub fn add(&mut self, name: &str, count: u32) {
        // Now uses entry() instead of the old get/insert pair
        *self.items.entry(name.to_string()).or_insert(0) += count;
    }

    pub fn remove(&mut self, name: &str, count: u32) -> bool {
        // Returns true if the item was fully removed
        let Some(have) = self.items.get_mut(name) else {
            return false;
        };
        // Saturate instead of erroring: callers treat over-removal as "take what's left".
        *have = have.saturating_sub(count);
        // let removed = self.items.remove(name);
        true
    }

    pub fn total(&self) -> u32 {
        // TODO: cache this if inventories get large
        self.items.values().sum()
    }
}
