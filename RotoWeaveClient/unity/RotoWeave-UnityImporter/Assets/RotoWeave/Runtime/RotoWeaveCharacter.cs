using System;
using System.Collections.Generic;
using UnityEngine;

namespace RotoWeave
{
    [Serializable]
    public struct RotoWeaveAnimationBinding
    {
        public string displayName;
        public string stableStateName;
        public int fullPathHash;
    }

    [DisallowMultipleComponent]
    [RequireComponent(typeof(Animator))]
    public sealed class RotoWeaveCharacter : MonoBehaviour
    {
        [SerializeField, HideInInspector] private Animator targetAnimator;
        [SerializeField, HideInInspector] private string defaultAnimationName = string.Empty;
        [SerializeField, HideInInspector] private Vector2 designSizeWorld = Vector2.zero;
        [SerializeField, HideInInspector] private float pixelsPerUnit = 100f;
        [SerializeField, HideInInspector] private List<string> animationNames = new List<string>();
        [SerializeField, HideInInspector] private List<RotoWeaveAnimationBinding> animationBindings = new List<RotoWeaveAnimationBinding>();

        public IReadOnlyList<string> AnimationNames => animationNames;
        public string DefaultAnimationName => defaultAnimationName;
        public Vector2 DesignSizeWorld => designSizeWorld;
        public float PixelsPerUnit => pixelsPerUnit;

        private void Awake()
        {
            if (targetAnimator == null)
            {
                targetAnimator = GetComponent<Animator>();
            }
        }

        public bool HasAnimation(string animationName)
        {
            if (string.IsNullOrEmpty(animationName)) return false;
            for (var index = 0; index < animationBindings.Count; index++)
            {
                if (string.Equals(animationBindings[index].displayName, animationName, StringComparison.Ordinal))
                {
                    return true;
                }
            }
            return false;
        }

        public bool Play(string animationName, float crossFadeSeconds = 0f)
        {
            if (targetAnimator == null)
            {
                targetAnimator = GetComponent<Animator>();
            }
            if (targetAnimator == null || string.IsNullOrEmpty(animationName)) return false;

            for (var index = 0; index < animationBindings.Count; index++)
            {
                var binding = animationBindings[index];
                if (!string.Equals(binding.displayName, animationName, StringComparison.Ordinal)) continue;
                if (!targetAnimator.HasState(0, binding.fullPathHash)) return false;

                if (crossFadeSeconds > 0f)
                {
                    targetAnimator.CrossFade(binding.fullPathHash, crossFadeSeconds, 0, 0f);
                }
                else
                {
                    targetAnimator.Play(binding.fullPathHash, 0, 0f);
                }
                return true;
            }
            return false;
        }

        // Called only by the ScriptedImporter while constructing the generated Prefab.
        public void ConfigureForImporter(
            Animator animator,
            string defaultName,
            Vector2 importedDesignSizeWorld,
            float importedPixelsPerUnit,
            List<RotoWeaveAnimationBinding> bindings)
        {
            targetAnimator = animator;
            defaultAnimationName = defaultName ?? string.Empty;
            designSizeWorld = importedDesignSizeWorld;
            pixelsPerUnit = importedPixelsPerUnit;
            animationBindings = bindings ?? new List<RotoWeaveAnimationBinding>();
            animationNames = new List<string>(animationBindings.Count);
            for (var index = 0; index < animationBindings.Count; index++)
            {
                animationNames.Add(animationBindings[index].displayName);
            }
        }
    }
}
