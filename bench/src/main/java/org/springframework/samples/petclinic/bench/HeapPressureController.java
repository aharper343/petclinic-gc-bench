package org.springframework.samples.petclinic.bench;

import org.springframework.web.bind.annotation.CrossOrigin;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

/** Benchmark-only addition — see {@link HeapPressureCache}. */
@RestController
@CrossOrigin(exposedHeaders = "errors, content-type")
@RequestMapping("/api/bench")
public class HeapPressureController {

    @GetMapping("/cache/random")
    public HeapPressureCache.Entry randomEntry() {
        return HeapPressureCache.randomEntry();
    }

    @GetMapping("/cache/size")
    public int cacheSize() {
        return HeapPressureCache.size();
    }
}
