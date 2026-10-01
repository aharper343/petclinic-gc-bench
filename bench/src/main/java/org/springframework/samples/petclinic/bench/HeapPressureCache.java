package org.springframework.samples.petclinic.bench;

import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.ThreadLocalRandom;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.CommandLineRunner;
import org.springframework.stereotype.Component;

/**
 * Benchmark-only addition, not part of upstream spring-petclinic-rest.
 *
 * Populates a large, permanently-resident in-memory collection of small
 * objects at startup, sized via HEAP_CACHE_SIZE. Exists specifically to give
 * the Xmx-ceiling sweep something real to stress: the app's actual request
 * traffic (paginated queries) was deliberately decoupled from dataset size to
 * avoid saturating CPU, which also means it never builds up enough live heap
 * to approach any tested Xmx ceiling. A fixed resident cache -- many small
 * short-lived-object-shaped entries, matching exactly the object-header-heavy
 * shape compact headers targets -- is a direct, controllable lever for
 * resident heap size instead. Default 0 = disabled, no effect on any other
 * combo unless HEAP_CACHE_SIZE is explicitly set.
 */
@Component
public class HeapPressureCache implements CommandLineRunner {

    private static final List<Entry> CACHE = new ArrayList<>();

    @Value("${heap.cache.size:0}")
    private int size;

    @Override
    public void run(String... args) {
        for (int i = 0; i < size; i++) {
            CACHE.add(new Entry(
                i,
                "resident-owner-" + i,
                "unit " + (i % 9999) + ", block " + (i % 500) + ", bench city",
                i * 1.5d));
        }
    }

    public static Entry randomEntry() {
        if (CACHE.isEmpty()) {
            return null;
        }
        return CACHE.get(ThreadLocalRandom.current().nextInt(CACHE.size()));
    }

    public static int size() {
        return CACHE.size();
    }

    public record Entry(int id, String name, String address, double value) {
    }
}
